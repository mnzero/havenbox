//! Candidate CLI boundary. A host approval registry must be injected by the trusted
//! supervisor, never by a harness. No production issuer or permissive default exists.
mod digest;
mod process;
use hb_csv_engine::{Engine, FactId, FileId};
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::sync::atomic::AtomicBool;

pub type Hash = [u8; 32];
pub fn sha256(bytes: &[u8]) -> Hash {
    digest::digest(bytes)
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Diagnostic {
    pub code: &'static str,
    pub status: Option<i32>,
    pub stdout_bytes: usize,
    pub stderr_bytes: usize,
    pub cleanup_confirmed: bool,
}
fn error(code: &'static str) -> Diagnostic {
    Diagnostic {
        code,
        status: None,
        stdout_bytes: 0,
        stderr_bytes: 0,
        cleanup_confirmed: true,
    }
}
pub type Result<T> = std::result::Result<T, Diagnostic>;
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Binding {
    pub task: String,
    pub session: String,
    pub provider: String,
    pub model: String,
    pub tool_revision: String,
    pub plan_digest: Hash,
    pub step: u64,
    pub epoch: u64,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Limits {
    pub total_bytes: usize,
    pub timeout_ms: u64,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Permissions {
    pub read: Vec<FileId>,
    pub publish: bool,
}
/// Untrusted request data, NOT a permit. All fields must match the host registry.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Approval {
    pub issuer: String,
    pub call_id: String,
    pub binding: Binding,
    pub arguments_digest: Hash,
    pub template_digest: Hash,
    pub permissions: Permissions,
    pub limits: Limits,
    pub not_before: u64,
    pub expires_at: u64,
    pub result_hash: Hash,
    pub result_rows: usize,
    pub result_header: Vec<String>,
    pub result_sources: Vec<FileId>,
}
/// Trusted supervisor integration seam: atomically compare the ENTIRE approval
/// and proof against the host-owned record, check revocation, and consume call ID.
/// Implementations must not trust request-provided issuer names or hash checks.
/// No implementation is shipped until the host registry is connected (HB-19).
pub trait HostVerifier {
    fn consume(&mut self, approval: &Approval, proof: &[u8], now: u64) -> Result<()>;
}
pub struct UnconnectedHost;
impl HostVerifier for UnconnectedHost {
    fn consume(&mut self, _: &Approval, _: &[u8], _: u64) -> Result<()> {
        Err(error("host_unconnected"))
    }
}
// Intentionally neither public nor constructible/deserializable by callers.
struct CallPermit {
    approval: Approval,
    argv: Vec<String>,
}
#[derive(Debug, Clone)]
pub enum Argument {
    Enum(String),
    Integer(u32),
    File(FileId),
}
/// Exact reviewed template snapshot. Arbitrary executable/schema substitutions
/// are rejected, including changed templates with the same executable and cwd.
pub struct Template {
    executable: PathBuf,
    cwd: PathBuf,
    digest: Hash,
}
const TEMPLATE: &[u8] = include_bytes!("../../../config/cli/template.json");
impl Template {
    pub fn load(bytes: &[u8]) -> Result<Self> {
        if bytes != TEMPLATE {
            return Err(error("template_revision"));
        }
        Ok(Self {
            executable: "/usr/local/lib/havenbox/csv-engine".into(),
            cwd: "/workspace/task".into(),
            digest: sha256(bytes),
        })
    }
    fn arguments(
        &self,
        args: &[Argument],
        files: &BTreeMap<FileId, PathBuf>,
        engine: &Engine,
    ) -> Result<(Vec<String>, Vec<FileId>)> {
        let [
            Argument::File(id),
            Argument::Enum(format),
            Argument::Integer(rows),
        ] = args
        else {
            return Err(error("argument_schema"));
        };
        if format != "csv" || !(1..=200).contains(rows) {
            return Err(error("argument_schema"));
        }
        let path = files.get(id).ok_or_else(|| error("file_id"))?;
        if path.parent() != Some(self.cwd.as_path()) || !fixed(path, false) {
            return Err(error("file_path"));
        }
        let expected = engine.csv_bytes(*id).map_err(|_| error("file_id"))?;
        // Only a trusted, immutable guest input staging area is acceptable. The
        // real mount/TOCTOU boundary is pending HB-11; this is not a host sandbox.
        let metadata = std::fs::metadata(path).map_err(|_| error("file_io"))?;
        if metadata.len() != expected.len() as u64 {
            return Err(error("input_hash"));
        }
        let bytes = std::fs::read(path).map_err(|_| error("file_io"))?;
        if sha256(&bytes) != sha256(&expected) {
            return Err(error("input_hash"));
        }
        Ok((
            vec![
                "inspect".into(),
                "--input".into(),
                path.to_str().ok_or_else(|| error("file_path"))?.into(),
                "--format".into(),
                format.clone(),
                "--rows".into(),
                rows.to_string(),
            ],
            vec![*id],
        ))
    }
}
fn fixed(path: &Path, directory: bool) -> bool {
    path.is_absolute()
        && path.components().all(|c| {
            !matches!(
                c,
                std::path::Component::ParentDir | std::path::Component::CurDir
            )
        })
        && std::fs::symlink_metadata(path)
            .is_ok_and(|m| if directory { m.is_dir() } else { m.is_file() })
        && std::fs::canonicalize(path).ok().as_deref() == Some(path)
}
/// Length-prefixed UTF-8 array; boundaries cannot collide (no shell encoding).
pub fn arguments_digest(argv: &[String]) -> Hash {
    let mut bytes = b"hb.cli.argv.v1\0".to_vec();
    for arg in argv {
        bytes.extend_from_slice(&(arg.len() as u64).to_be_bytes());
        bytes.extend_from_slice(arg.as_bytes());
    }
    sha256(&bytes)
}
pub struct Dispatcher {
    binding: Binding,
    used: BTreeSet<String>,
}
impl Dispatcher {
    pub fn new(binding: Binding) -> Self {
        Self {
            binding,
            used: BTreeSet::new(),
        }
    }
    fn admit(
        &mut self,
        template: &Template,
        approval: &Approval,
        argv: Vec<String>,
        sources: &[FileId],
        proof: &[u8],
        now: u64,
        host: &mut dyn HostVerifier,
    ) -> Result<CallPermit> {
        if approval.binding != self.binding {
            return Err(error("binding"));
        }
        if approval.issuer.is_empty() || approval.call_id.is_empty() || proof.is_empty() {
            return Err(error("proof"));
        }
        if now < approval.not_before || now >= approval.expires_at {
            return Err(error("expired"));
        }
        if self.used.contains(&approval.call_id) {
            return Err(error("replay"));
        }
        if approval.arguments_digest != arguments_digest(&argv) {
            return Err(error("arguments_digest"));
        }
        if approval.template_digest != template.digest {
            return Err(error("template_digest"));
        }
        if !approval.permissions.publish
            || approval.permissions.read != sources
            || approval.result_sources != sources
        {
            return Err(error("permissions"));
        }
        if approval.limits.total_bytes == 0
            || approval.limits.total_bytes > 1048576
            || approval.limits.timeout_ms == 0
            || approval.limits.timeout_ms > 60000
        {
            return Err(error("limits"));
        }
        host.consume(approval, proof, now)?;
        self.used.insert(approval.call_id.clone()); // fail-closed even after spawn/result failure
        Ok(CallPermit {
            approval: approval.clone(),
            argv,
        })
    }
    pub fn execute(
        &mut self,
        template: &Template,
        approval: &Approval,
        args: &[Argument],
        files: &BTreeMap<FileId, PathBuf>,
        inputs: &Engine,
        proof: &[u8],
        now: u64,
        host: &mut dyn HostVerifier,
        cancelled: &AtomicBool,
    ) -> Result<Published> {
        let (argv, sources) = template.arguments(args, files, inputs)?;
        let permit = self.admit(template, approval, argv, &sources, proof, now, host)?;
        if !fixed(&template.executable, false) || !fixed(&template.cwd, true) {
            return Err(error("execution_path"));
        }
        let output = process::run(
            &template.executable,
            &template.cwd,
            &permit.argv,
            &permit.approval.limits,
            cancelled,
        )?;
        publish(&permit, output)
    }
}
/// Transactional publication: a private engine is dropped on any error; callers
/// never observe partially registered artifacts/facts. Provenance is host-bound.
pub struct Published {
    // Keep the host-approved bytes immutable: Engine serialization normalizes
    // CSV syntax and must never replace the artifact bound by result_hash.
    stdout: Vec<u8>,
    engine: Engine,
    pub file: FileId,
    pub fact: FactId,
    pub content_hash: Hash,
    pub provenance: Approval,
}
impl Published {
    pub fn csv_bytes(&self) -> Result<Vec<u8>> {
        Ok(self.stdout.clone())
    }
    pub fn report(&self) -> Result<String> {
        self.engine
            .write_report(&[self.fact])
            .map_err(|_| error("fact"))
    }
}
fn publish(permit: &CallPermit, output: process::Output) -> Result<Published> {
    let fail = |code| Diagnostic {
        code,
        status: Some(output.status),
        stdout_bytes: output.stdout.len(),
        stderr_bytes: output.stderr.len(),
        cleanup_confirmed: true,
    };
    if output.status != 0 {
        return Err(fail("exit"));
    }
    let a = &permit.approval;
    if sha256(&output.stdout) != a.result_hash {
        return Err(fail("result_hash"));
    }
    let mut limits = hb_csv_engine::Limits::default();
    limits.file_bytes = a.limits.total_bytes;
    limits.total_bytes = a.limits.total_bytes;
    let mut engine = Engine::new(limits);
    let file = engine
        .register_input(&output.stdout)
        .map_err(|_| fail("result_schema"))?;
    let inspection = engine
        .inspect_csv(file)
        .map_err(|_| fail("result_schema"))?;
    if inspection.header != a.result_header {
        return Err(fail("result_schema"));
    }
    if inspection.rows != a.result_rows {
        return Err(fail("result_incomplete"));
    }
    Ok(Published {
        stdout: output.stdout,
        engine,
        file,
        fact: inspection.fact,
        content_hash: a.result_hash,
        provenance: a.clone(),
    })
}
#[cfg(test)]
mod tests;
