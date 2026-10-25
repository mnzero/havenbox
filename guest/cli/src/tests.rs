use super::*;
struct TestIssuer {
    record: Approval,
    used: bool,
}
impl HostVerifier for TestIssuer {
    fn consume(&mut self, a: &Approval, proof: &[u8], _: u64) -> Result<()> {
        if a != &self.record || proof != b"test-only-proof" || self.used {
            return Err(error("host_record"));
        }
        self.used = true;
        Ok(())
    }
}
fn fixture() -> (Template, Approval, Vec<String>, TestIssuer) {
    let t = Template::load(TEMPLATE).unwrap();
    let argv = vec!["ok".into()];
    let a = Approval {
        issuer: "host-test".into(),
        call_id: "call_1".into(),
        binding: Binding {
            task: "task_1".into(),
            session: "session_1".into(),
            provider: "local".into(),
            model: "model_1".into(),
            tool_revision: "v1".into(),
            plan_digest: [1; 32],
            step: 1,
            epoch: 1,
        },
        arguments_digest: arguments_digest(&argv),
        template_digest: t.digest,
        permissions: Permissions {
            read: vec![],
            publish: true,
        },
        limits: Limits {
            total_bytes: 1024,
            timeout_ms: 1000,
        },
        not_before: 10,
        expires_at: 20,
        result_hash: sha256(b"name\na\n"),
        result_rows: 1,
        result_header: vec!["name".into()],
        result_sources: vec![],
    };
    let host = TestIssuer {
        record: a.clone(),
        used: false,
    };
    (t, a, argv, host)
}
#[test]
fn negative_approval_matrix() {
    let mutations: Vec<Box<dyn Fn(&mut Approval)>> = vec![
        Box::new(|a| a.issuer = "forged".into()),
        Box::new(|a| a.call_id = "other".into()),
        Box::new(|a| a.binding.task.push('x')),
        Box::new(|a| a.binding.session.push('x')),
        Box::new(|a| a.binding.provider.push('x')),
        Box::new(|a| a.binding.model.push('x')),
        Box::new(|a| a.binding.tool_revision.push('x')),
        Box::new(|a| a.binding.plan_digest[0] ^= 1),
        Box::new(|a| a.binding.step += 1),
        Box::new(|a| a.binding.epoch += 1),
        Box::new(|a| a.arguments_digest[0] ^= 1),
        Box::new(|a| a.template_digest[0] ^= 1),
        Box::new(|a| a.permissions.publish = false),
        Box::new(|a| a.limits.total_bytes = 0),
        Box::new(|a| a.limits.timeout_ms = 60001),
        Box::new(|a| a.expires_at = 15),
        Box::new(|a| a.not_before = 16),
        Box::new(|a| a.result_hash[0] ^= 1),
        Box::new(|a| a.result_rows = 3),
        Box::new(|a| a.result_header = vec!["forged".into()]),
        Box::new(|a| a.limits.total_bytes = 100),
    ];
    for mutate in mutations {
        let (t, mut a, argv, mut host) = fixture();
        let mut dispatcher = Dispatcher::new(a.binding.clone());
        mutate(&mut a);
        assert!(
            dispatcher
                .admit(&t, &a, argv, &[], b"test-only-proof", 15, &mut host)
                .is_err()
        );
    }
}
#[test]
fn replay_proof_and_missing_host() {
    let (t, a, argv, mut host) = fixture();
    let mut d = Dispatcher::new(a.binding.clone());
    assert!(
        d.admit(&t, &a, argv.clone(), &[], b"forged", 15, &mut host)
            .is_err()
    );
    assert!(
        d.admit(
            &t,
            &a,
            argv.clone(),
            &[],
            b"test-only-proof",
            15,
            &mut UnconnectedHost
        )
        .is_err()
    );
    d.admit(&t, &a, argv.clone(), &[], b"test-only-proof", 15, &mut host)
        .unwrap();
    assert!(
        d.admit(&t, &a, argv.clone(), &[], b"test-only-proof", 15, &mut host)
            .is_err()
    );
    let mut fresh = Dispatcher::new(a.binding.clone());
    assert!(
        fresh
            .admit(&t, &a, argv, &[], b"test-only-proof", 15, &mut host)
            .is_err()
    );
}
#[test]
fn template_and_arguments_reject_untyped_input() {
    let (t, _, _, _) = fixture();
    let e = Engine::new(Default::default());
    for arg in [
        "../secret",
        "/etc/passwd",
        "$(echo x)",
        "a;echo x",
        "x\n",
        "filter(x)",
    ] {
        assert!(
            t.arguments(&[Argument::Enum(arg.into())], &BTreeMap::new(), &e)
                .is_err()
        );
    }
    let changed = String::from_utf8(TEMPLATE.to_vec())
        .unwrap()
        .replace("inspect", "execute");
    assert!(Template::load(changed.as_bytes()).is_err());
    assert_ne!(
        arguments_digest(&["ab".into(), "c".into()]),
        arguments_digest(&["a".into(), "bc".into()])
    );
}
#[test]
fn file_id_mapping_and_schema() {
    let mut e = Engine::new(Default::default());
    let id = e.register_input(b"name\na\n").unwrap();
    let dir = std::env::temp_dir().join(format!("hb-cli-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let dir = std::fs::canonicalize(dir).unwrap();
    let path = dir.join("input.csv");
    std::fs::write(&path, e.csv_bytes(id).unwrap()).unwrap();
    let mut t = Template::load(TEMPLATE).unwrap();
    t.cwd = dir.clone();
    let files = BTreeMap::from([(id, path.clone())]);
    let args = [
        Argument::File(id),
        Argument::Enum("csv".into()),
        Argument::Integer(1),
    ];
    assert!(t.arguments(&args, &files, &e).is_ok());
    assert!(
        t.arguments(&args, &files, &Engine::new(Default::default()))
            .is_err()
    );
    assert!(t.arguments(&args, &BTreeMap::new(), &e).is_err());
    for format in ["sh", "../", "csv;pwd"] {
        assert!(
            t.arguments(
                &[
                    Argument::File(id),
                    Argument::Enum(format.into()),
                    Argument::Integer(1)
                ],
                &files,
                &e
            )
            .is_err()
        );
    }
    for rows in [0, 201, u32::MAX] {
        assert!(
            t.arguments(
                &[
                    Argument::File(id),
                    Argument::Enum("csv".into()),
                    Argument::Integer(rows)
                ],
                &files,
                &e
            )
            .is_err()
        );
    }
    std::fs::write(&path, b"name\nb\n").unwrap();
    assert!(t.arguments(&args, &files, &e).is_err());
    std::fs::remove_dir_all(dir).unwrap();
}
#[test]
fn deterministic_echo_and_bounded_capture() {
    let cwd = std::env::current_dir().unwrap();
    let cancelled = AtomicBool::new(false);
    let limits = Limits {
        total_bytes: 200000,
        timeout_ms: 2000,
    };
    let args = vec!["x".repeat(1000); 80]; // exceeds typical pipe capacity
    let output = process::run(Path::new("/bin/echo"), &cwd, &args, &limits, &cancelled).unwrap();
    assert_eq!(output.stdout.len(), 80080);
    let small = Limits {
        total_bytes: 10,
        timeout_ms: 2000,
    };
    assert_eq!(
        process::run(Path::new("/bin/echo"), &cwd, &args, &small, &cancelled)
            .err()
            .unwrap()
            .code,
        "output_limit"
    );
    cancelled.store(true, std::sync::atomic::Ordering::Release);
    assert_eq!(
        process::run(Path::new("/bin/echo"), &cwd, &args, &limits, &cancelled)
            .err()
            .unwrap()
            .code,
        "cancelled"
    );
    cancelled.store(false, std::sync::atomic::Ordering::Release);
    assert_eq!(
        process::run(
            Path::new("/bin/echo"),
            &cwd,
            &args,
            &Limits {
                total_bytes: 10,
                timeout_ms: 0
            },
            &cancelled
        )
        .err()
        .unwrap()
        .code,
        "timeout"
    );
}
#[test]
fn published_bytes_and_provenance_preserve_approved_stdout() {
    for bytes in [
        b"name\na\n".as_slice(),
        b"name\r\na\r\n",
        b"\xef\xbb\xbfname\na\n",
        b"\"name\"\n\"a\"\n",
    ] {
        let (t, mut approval, argv, mut host) = fixture();
        approval.result_hash = sha256(bytes);
        host.record = approval.clone();
        let permit = Dispatcher::new(approval.binding.clone())
            .admit(&t, &approval, argv, &[], b"test-only-proof", 15, &mut host)
            .unwrap();
        let output = |stdout: &[u8]| process::Output {
            stdout: stdout.to_vec(),
            stderr: vec![],
            status: 0,
        };
        let published = publish(&permit, output(bytes)).unwrap();
        let mut retrieved = published.csv_bytes().unwrap();
        assert_eq!(retrieved, bytes);
        assert_eq!(sha256(&retrieved), published.content_hash);
        assert_eq!(sha256(&retrieved), published.provenance.result_hash);
        assert_eq!(published.provenance, approval);
        assert!(published.report().unwrap().contains('1'));
        // A caller's copy cannot change the stored, approved artifact.
        retrieved.clear();
        assert_eq!(published.csv_bytes().unwrap(), bytes);
        let changed = [bytes, b"b\n"].concat();
        assert_eq!(
            publish(&permit, output(&changed)).err().unwrap().code,
            "result_hash"
        );
        // Equivalent parsed data must not stand in for different approved bytes.
        if bytes != b"name\na\n" {
            assert_eq!(
                publish(&permit, output(b"name\na\n")).err().unwrap().code,
                "result_hash"
            );
        }
    }
}
#[test]
fn exit_zero_is_not_artifact_success() {
    let (_, a, argv, _) = fixture();
    let mut p = CallPermit { approval: a, argv };
    let output = |bytes: &[u8], status| process::Output {
        stdout: bytes.to_vec(),
        stderr: b"diagnostic".to_vec(),
        status,
    };
    let result = publish(&p, output(b"name\na\n", 0)).unwrap();
    assert_eq!(result.csv_bytes().unwrap(), b"name\na\n");
    assert!(result.report().unwrap().contains('1'));
    let err = publish(&p, output(b"name\na\n", 7)).err().unwrap();
    assert_eq!(
        (err.code, err.status, err.stderr_bytes),
        ("exit", Some(7), 10)
    );
    assert_eq!(
        publish(&p, output(b"wrong", 0)).err().unwrap().code,
        "result_hash"
    );
    p.approval.result_rows = 2;
    assert_eq!(
        publish(&p, output(b"name\na\n", 0)).err().unwrap().code,
        "result_incomplete"
    );
    p.approval.result_hash = sha256(b"a,a\nx,y\n");
    assert_eq!(
        publish(&p, output(b"a,a\nx,y\n", 0)).err().unwrap().code,
        "result_schema"
    );
}
