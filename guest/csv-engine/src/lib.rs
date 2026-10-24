//! Deterministic, capability-only CSV operations. No filesystem, network, or evaluator.
mod spill;
pub use spill::Scratch;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::{
    Arc,
    atomic::{AtomicBool, AtomicU64, Ordering},
};

static NEXT_ISSUER: AtomicU64 = AtomicU64::new(1);

pub const VERSION: &str = "hb.csv-engine.v1-candidate";
const MAX_MONEY: i64 = 999_999_999_999_999_999;
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Error {
    pub code: &'static str,
    pub rows: Vec<usize>,
    pub missing_columns: Vec<String>,
    pub extra_columns: Vec<String>,
    pub header_order_matches: Option<bool>,
    pub duplicate_headers: Vec<String>,
}
pub type Result<T> = std::result::Result<T, Error>;
fn err(code: &'static str) -> Error {
    Error {
        code,
        rows: vec![],
        missing_columns: vec![],
        extra_columns: vec![],
        header_order_matches: None,
        duplicate_headers: vec![],
    }
}
#[derive(Debug, Clone)]
pub struct Limits {
    pub files: usize,
    pub file_bytes: usize,
    pub total_bytes: usize,
    pub rows: usize,
    pub columns: usize,
    pub cell_bytes: usize,
    pub memory_bytes: usize,
    pub aggregate_memory_bytes: usize,
    pub groups: usize,
    pub preview_rows: usize,
    pub preview_bytes: usize,
    pub output_bytes: usize,
}
impl Default for Limits {
    fn default() -> Self {
        Self {
            files: 20,
            file_bytes: 50 * 1024 * 1024,
            total_bytes: 200 * 1024 * 1024,
            rows: 1_000_000,
            columns: 256,
            cell_bytes: 1024 * 1024,
            memory_bytes: 512 * 1024 * 1024,
            aggregate_memory_bytes: 64 * 1024 * 1024,
            groups: 100_000,
            preview_rows: 200,
            preview_bytes: 2 * 1024 * 1024,
            output_bytes: 50 * 1024 * 1024,
        }
    }
}
/// Issued only by this engine; IDs cannot be interpreted as paths.
#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub struct FileId {
    issuer: u64,
    generation: u64,
    index: usize,
}
#[derive(Clone, Copy, PartialEq, Eq)]
pub struct FactId {
    issuer: u64,
    generation: u64,
    index: usize,
}
impl std::fmt::Debug for FileId {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_tuple("FileId").field(&self.index).finish()
    }
}
impl std::fmt::Debug for FactId {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_tuple("FactId").field(&self.index).finish()
    }
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Money(i64);
impl Money {
    pub fn parse(s: &str) -> Result<Self> {
        let unsigned = s.strip_prefix('-').unwrap_or(s);
        let parts: Vec<_> = unsigned.split('.').collect();
        let whole = parts[0];
        if parts.len() > 2
            || whole.is_empty()
            || whole.len() > 16
            || !whole.bytes().all(|b| b.is_ascii_digit())
            || (whole.len() > 1 && whole.starts_with('0'))
        {
            return Err(err("invalid_numeric"));
        }
        let fraction = if parts.len() == 2 { parts[1] } else { "" };
        if parts.len() == 2
            && (fraction.is_empty()
                || fraction.len() > 2
                || !fraction.bytes().all(|b| b.is_ascii_digit()))
        {
            return Err(err("precision_or_numeric"));
        }
        let mut n = whole.parse::<i64>().map_err(|_| err("overflow"))? * 100;
        if !fraction.is_empty() {
            n += fraction
                .parse::<i64>()
                .map_err(|_| err("invalid_numeric"))?
                * if fraction.len() == 1 { 10 } else { 1 };
        }
        if s.starts_with('-') {
            n = -n;
        }
        Ok(Self(n))
    }
    fn add(&self, other: &Self) -> Result<Self> {
        let n = self.0.checked_add(other.0).ok_or_else(|| err("overflow"))?;
        if !(-MAX_MONEY..=MAX_MONEY).contains(&n) {
            return Err(err("overflow"));
        }
        Ok(Self(n))
    }
    pub fn decimal(&self) -> String {
        format!(
            "{}{}.{:02}",
            if self.0 < 0 { "-" } else { "" },
            self.0.abs() / 100,
            self.0.abs() % 100
        )
    }
}
#[derive(Debug, Clone)]
struct Table {
    header: Vec<String>,
    rows: Vec<Vec<String>>,
    sources: Vec<FileId>,
    bom: bool,
    generation: u64,
}
#[derive(Debug, Clone)]
pub enum Compare {
    Eq,
    Ne,
    Lt,
    Le,
    Gt,
    Ge,
}
#[derive(Debug, Clone)]
pub enum Value {
    Text(String),
    Decimal(Money),
}
#[derive(Debug, Clone)]
pub enum DuplicateMode {
    AllMembers,
    AfterFirst,
}
#[derive(Debug, Clone)]
pub enum Condition {
    Compare {
        column: String,
        op: Compare,
        value: Value,
    },
    DuplicateKey {
        columns: Vec<String>,
        mode: DuplicateMode,
    },
}
#[derive(Debug, Clone)]
pub enum Operation {
    Inspect {
        input: FileId,
    },
    Concat {
        inputs: Vec<FileId>,
    },
    Filter {
        input: FileId,
        condition: Condition,
    },
    Aggregate {
        input: FileId,
        group_by: Vec<String>,
        sum: String,
    },
    Report {
        facts: Vec<FactId>,
    },
}
#[derive(Debug, Clone)]
pub struct Fact {
    pub operation: Operation,
    pub sources: Vec<FileId>,
    pub input_rows: usize,
    pub output_rows: usize,
    pub excluded_rows: usize,
    pub parse_error_rows: usize,
    pub sum: Option<Money>,
    pub artifact_refs: Vec<FileId>,
    generation: u64,
}
#[derive(Debug)]
pub struct Inspection {
    pub header: Vec<String>,
    pub type_candidates: Vec<&'static str>,
    pub samples: Vec<Vec<String>>,
    pub rows: usize,
    pub utf8_bom: bool,
    pub fact: FactId,
}
#[derive(Debug, PartialEq, Eq)]
pub struct KeyIssues {
    pub duplicates: BTreeMap<Vec<String>, Vec<usize>>,
    pub empty_rows: Vec<usize>,
}
/// Input bytes are supplied by the future supervisor's controlled file channel.
/// This crate never opens paths or publishes files. All results commit atomically in memory.
/// Aggregation scratch is optional and supplied solely by the trusted caller.
pub struct Engine {
    limits: Limits,
    tables: Vec<Table>,
    facts: Vec<Fact>,
    input_count: usize,
    input_bytes: usize,
    retained: usize,
    scratch: Option<Scratch>,
    cancelled: Arc<AtomicBool>,
    issuer: u64,
    next_generation: u64,
}
impl Engine {
    pub fn try_new(limits: Limits) -> Result<Self> {
        let issuer = NEXT_ISSUER
            .try_update(Ordering::Relaxed, Ordering::Relaxed, |n| n.checked_add(1))
            .map_err(|_| err("issuer_exhausted"))?;
        Ok(Self {
            limits,
            tables: vec![],
            facts: vec![],
            input_count: 0,
            input_bytes: 0,
            retained: 0,
            scratch: None,
            cancelled: Arc::new(AtomicBool::new(false)),
            issuer,
            next_generation: 0,
        })
    }
    pub fn new(limits: Limits) -> Self {
        Self::try_new(limits).expect("engine issuer exhausted")
    }
    fn issue_file(&mut self) -> Result<FileId> {
        let generation = self.next_generation;
        self.next_generation = self
            .next_generation
            .checked_add(1)
            .ok_or_else(|| err("id_exhausted"))?;
        Ok(FileId {
            issuer: self.issuer,
            generation,
            index: self.tables.len(),
        })
    }
    fn issue_fact(&mut self) -> Result<FactId> {
        let generation = self.next_generation;
        self.next_generation = self
            .next_generation
            .checked_add(1)
            .ok_or_else(|| err("id_exhausted"))?;
        Ok(FactId {
            issuer: self.issuer,
            generation,
            index: self.facts.len(),
        })
    }
    pub fn with_scratch(mut self, scratch: Scratch) -> Self {
        self.scratch = Some(scratch);
        self
    }
    pub fn cancellation_handle(&self) -> Arc<AtomicBool> {
        self.cancelled.clone()
    }
    fn active(&self) -> Result<()> {
        if self.cancelled.load(Ordering::Relaxed) {
            Err(err("cancelled"))
        } else {
            Ok(())
        }
    }
    fn spill(&self) -> Result<spill::Spill> {
        self.scratch
            .as_ref()
            .ok_or_else(|| err("aggregate_memory_limit"))?
            .open(self.limits.aggregate_memory_bytes, self.cancelled.clone())
    }
    pub fn artifact_count(&self) -> usize {
        self.tables.len()
    }
    pub fn fact_count(&self) -> usize {
        self.facts.len()
    }
    pub fn fact(&self, id: FactId) -> Result<&Fact> {
        if id.issuer != self.issuer {
            return Err(err("foreign_fact"));
        }
        let fact = self
            .facts
            .get(id.index)
            .ok_or_else(|| err("unknown_fact"))?;
        if id.generation != fact.generation {
            return Err(err("foreign_fact"));
        }
        Ok(fact)
    }
    fn table(&self, id: FileId) -> Result<&Table> {
        if id.issuer != self.issuer {
            return Err(err("foreign_file"));
        }
        let table = self
            .tables
            .get(id.index)
            .ok_or_else(|| err("unknown_file"))?;
        if id.generation != table.generation {
            return Err(err("foreign_file"));
        }
        Ok(table)
    }
    fn budget(&self, extra: usize) -> Result<()> {
        self.active()?;
        if extra > self.limits.memory_bytes.saturating_sub(self.retained) {
            Err(err("memory_limit"))
        } else {
            Ok(())
        }
    }
    pub fn register_input(&mut self, bytes: &[u8]) -> Result<FileId> {
        if self.input_count >= self.limits.files
            || bytes.len() > self.limits.file_bytes
            || bytes.len() > self.limits.total_bytes.saturating_sub(self.input_bytes)
        {
            return Err(err("file_limit"));
        }
        // Conservative allocation reservation includes String/Vec overhead, parser and copy peaks.
        self.budget(
            bytes
                .len()
                .checked_mul(128)
                .ok_or_else(|| err("memory_limit"))?,
        )?;
        let mut table = parse(bytes, &self.limits)?;
        let id = self.issue_file()?;
        table.sources.push(id);
        table.generation = id.generation;
        let size = table_size(&table);
        self.budget(size)?;
        self.tables.push(table);
        self.retained = self
            .retained
            .checked_add(size)
            .ok_or_else(|| err("memory_limit"))?;
        self.input_count = self
            .input_count
            .checked_add(1)
            .ok_or_else(|| err("file_limit"))?;
        self.input_bytes += bytes.len();
        Ok(id)
    }
    fn record(&mut self, mut fact: Fact) -> Result<FactId> {
        let cost = fact_cost(&fact).ok_or_else(|| err("memory_limit"))?;
        self.budget(cost)?;
        let id = self.issue_fact()?;
        fact.generation = id.generation;
        self.facts.push(fact);
        self.retained = self
            .retained
            .checked_add(cost)
            .ok_or_else(|| err("memory_limit"))?;
        Ok(id)
    }
    fn publish(&mut self, mut table: Table, mut fact: Fact) -> Result<(FileId, FactId)> {
        if table.rows.len() > self.limits.rows {
            return Err(err("row_limit"));
        }
        let size = table_size(&table);
        let fact_size = fact_cost(&fact).ok_or_else(|| err("memory_limit"))?;
        self.budget(
            size.checked_add(4096)
                .and_then(|n| n.checked_add(fact_size))
                .ok_or_else(|| err("memory_limit"))?,
        )?;
        let id = self.issue_file()?;
        fact.artifact_refs.push(id);
        table.generation = id.generation;
        let fid = self.record(fact)?;
        self.tables.push(table);
        self.retained = self
            .retained
            .checked_add(size)
            .ok_or_else(|| err("memory_limit"))?;
        Ok((id, fid))
    }
    pub fn inspect_csv(&mut self, input: FileId) -> Result<Inspection> {
        let t = self.table(input)?;
        let mut samples = vec![];
        let mut bytes = 0;
        for row in t.rows.iter().take(self.limits.preview_rows) {
            let size = row.iter().map(|x| x.len() + 24).sum::<usize>();
            if bytes + size > self.limits.preview_bytes {
                break;
            }
            bytes += size;
            samples.push(row.clone());
        }
        self.budget(bytes + table_size(t).min(self.limits.preview_bytes))?;
        let types = (0..t.header.len())
            .map(|i| {
                if !t.rows.is_empty() && t.rows.iter().all(|r| Money::parse(&r[i]).is_ok()) {
                    "decimal(scale=2)"
                } else {
                    "text"
                }
            })
            .collect();
        let mut out = Inspection {
            header: t.header.clone(),
            type_candidates: types,
            samples,
            rows: t.rows.len(),
            utf8_bom: t.bom,
            fact: FactId {
                issuer: self.issuer,
                generation: 0,
                index: 0,
            },
        };
        out.fact = self.record(make_fact(
            Operation::Inspect { input },
            t,
            t.rows.len(),
            0,
            None,
        ))?;
        Ok(out)
    }
    pub fn concat_csv(&mut self, inputs: &[FileId]) -> Result<(FileId, FactId)> {
        if inputs.is_empty() || inputs.len() > self.limits.files {
            return Err(err("file_limit"));
        }
        let first = self.table(inputs[0])?;
        let mut size = 0;
        let mut count = 0;
        for id in inputs {
            let t = self.table(*id)?;
            if t.header != first.header {
                let missing = first
                    .header
                    .iter()
                    .filter(|h| !t.header.contains(h))
                    .cloned()
                    .collect();
                let extra = t
                    .header
                    .iter()
                    .filter(|h| !first.header.contains(h))
                    .cloned()
                    .collect();
                return Err(Error {
                    code: "schema_mismatch",
                    rows: vec![],
                    missing_columns: missing,
                    extra_columns: extra,
                    header_order_matches: Some(first.header == t.header),
                    duplicate_headers: duplicate_headers(&t.header),
                });
            }
            size += table_size(t);
            count += t.rows.len();
        }
        self.budget(size.checked_mul(2).ok_or_else(|| err("memory_limit"))?)?;
        if count > self.limits.rows {
            return Err(err("row_limit"));
        }
        let mut t = Table {
            header: first.header.clone(),
            rows: vec![],
            sources: vec![],
            bom: false,
            generation: u64::MAX,
        };
        for id in inputs {
            let src = self.table(*id)?;
            t.rows.extend(src.rows.clone());
            for s in &src.sources {
                if !t.sources.contains(s) {
                    t.sources.push(*s);
                }
            }
        }
        let f = make_fact(
            Operation::Concat {
                inputs: inputs.to_vec(),
            },
            &t,
            count,
            0,
            None,
        );
        self.publish(t, f)
    }
    pub fn filter_rows(&mut self, input: FileId, condition: Condition) -> Result<(FileId, FactId)> {
        let t = self.table(input)?;
        let condition_cost = match &condition {
            Condition::Compare { column, value, .. } => column
                .len()
                .checked_add(match value {
                    Value::Text(v) => v.len(),
                    Value::Decimal(_) => 32,
                })
                .ok_or_else(|| err("memory_limit"))?,
            Condition::DuplicateKey { columns, .. } => columns
                .iter()
                .try_fold(0usize, |n, s| n.checked_add(s.len()))
                .ok_or_else(|| err("memory_limit"))?,
        };
        self.budget(
            table_size(t)
                .checked_mul(4)
                .and_then(|n| n.checked_add(condition_cost))
                .ok_or_else(|| err("memory_limit"))?,
        )?;
        let mut selected = Vec::with_capacity(t.rows.len().min(self.limits.rows));
        match &condition {
            Condition::Compare { column, op, value } => {
                let i = column_index(t, column)?;
                let mut invalid = vec![];
                for (n, row) in t.rows.iter().enumerate() {
                    let order = match value {
                        Value::Text(v) => row[i].cmp(v),
                        Value::Decimal(v) => match Money::parse(&row[i]) {
                            Ok(x) => x.0.cmp(&v.0),
                            Err(_) => {
                                invalid.push(n + 1);
                                continue;
                            }
                        },
                    };
                    let yes = match op {
                        Compare::Eq => order.is_eq(),
                        Compare::Ne => !order.is_eq(),
                        Compare::Lt => order.is_lt(),
                        Compare::Le => !order.is_gt(),
                        Compare::Gt => order.is_gt(),
                        Compare::Ge => !order.is_lt(),
                    };
                    if yes {
                        selected.push(row.clone());
                    }
                }
                if !invalid.is_empty() {
                    return Err(Error {
                        code: "invalid_numeric",
                        rows: invalid,
                        missing_columns: vec![],
                        extra_columns: vec![],
                        header_order_matches: None,
                        duplicate_headers: vec![],
                    });
                }
            }
            Condition::DuplicateKey { columns, mode } => {
                let indices = indices(t, columns)?;
                if table_size(t)
                    .checked_mul(2)
                    .ok_or_else(|| err("memory_limit"))?
                    > self.limits.aggregate_memory_bytes
                {
                    let mut disk = self.spill()?;
                    if matches!(mode, DuplicateMode::AllMembers) {
                        for row in &t.rows {
                            self.active()?;
                            disk.add(&key(row, &indices), &Money(0), self.limits.groups)?;
                        }
                    }
                    for row in &t.rows {
                        self.active()?;
                        let k = key(row, &indices);
                        let count = disk.count(&k)?;
                        let keep = match mode {
                            DuplicateMode::AllMembers => count > 1,
                            DuplicateMode::AfterFirst => {
                                disk.add(&k, &Money(0), self.limits.groups)?;
                                count > 0
                            }
                        };
                        if keep {
                            selected.push(row.clone());
                        }
                    }
                    disk.finish()?;
                } else {
                    let mut counts = BTreeMap::<Vec<String>, usize>::new();
                    for row in &t.rows {
                        self.active()?;
                        let k = key(row, &indices);
                        *counts.entry(k).or_default() += 1;
                        if counts.len() > self.limits.groups {
                            return Err(err("group_limit"));
                        }
                    }
                    let mut seen = BTreeSet::new();
                    for row in &t.rows {
                        self.active()?;
                        let k = key(row, &indices);
                        let keep = match mode {
                            DuplicateMode::AllMembers => counts[&k] > 1,
                            DuplicateMode::AfterFirst => !seen.insert(k),
                        };
                        if keep {
                            selected.push(row.clone());
                        }
                    }
                }
            }
        }
        let f = make_fact(
            Operation::Filter { input, condition },
            t,
            selected.len(),
            t.rows.len() - selected.len(),
            None,
        );
        let out = Table {
            header: t.header.clone(),
            rows: selected,
            sources: t.sources.clone(),
            bom: false,
            generation: u64::MAX,
        };
        self.publish(out, f)
    }
    pub fn check_keys(&self, input: FileId, columns: &[String]) -> Result<KeyIssues> {
        let t = self.table(input)?;
        self.budget(
            table_size(t)
                .checked_mul(4)
                .ok_or_else(|| err("memory_limit"))?,
        )?;
        let ix = indices(t, columns)?;
        let mut groups = BTreeMap::<Vec<String>, Vec<usize>>::new();
        let mut empty_rows = vec![];
        for (n, row) in t.rows.iter().enumerate() {
            let k = key(row, &ix);
            if k.iter().any(String::is_empty) {
                empty_rows.push(n + 1);
            }
            groups.entry(k).or_default().push(n + 1);
            if groups.len() > self.limits.groups {
                return Err(err("group_limit"));
            }
        }
        groups.retain(|_, v| v.len() > 1);
        Ok(KeyIssues {
            duplicates: groups,
            empty_rows,
        })
    }
    pub fn aggregate_csv(
        &mut self,
        input: FileId,
        group_by: &[String],
        sum: &str,
    ) -> Result<(FileId, FactId)> {
        let t = self.table(input)?;
        self.budget(
            table_size(t)
                .checked_mul(4)
                .ok_or_else(|| err("memory_limit"))?,
        )?;
        let ix = if group_by.is_empty() {
            vec![]
        } else {
            indices(t, group_by)?
        };
        let amount = column_index(t, sum)?;
        if group_by.iter().any(|s| s == "count" || s == "sum") {
            return Err(err("reserved_column"));
        }
        let mut disk = if table_size(t)
            .checked_mul(2)
            .ok_or_else(|| err("memory_limit"))?
            > self.limits.aggregate_memory_bytes
        {
            Some(self.spill()?)
        } else {
            None
        };
        let mut groups = BTreeMap::<Vec<String>, (usize, Money)>::new();
        let mut total = Money(0);
        let mut invalid = vec![];
        for (n, row) in t.rows.iter().enumerate() {
            self.active()?;
            let value = match Money::parse(&row[amount]) {
                Ok(v) => v,
                Err(_) => {
                    invalid.push(n + 1);
                    continue;
                }
            };
            total = total.add(&value)?;
            if let Some(disk) = disk.as_mut() {
                disk.add(&key(row, &ix), &value, self.limits.groups)?;
            } else {
                let entry = groups.entry(key(row, &ix)).or_insert((0, Money(0)));
                entry.0 += 1;
                entry.1 = entry.1.add(&value)?;
                if groups.len() > self.limits.groups {
                    return Err(err("group_limit"));
                }
            }
        }
        if !invalid.is_empty() {
            return Err(Error {
                code: "invalid_numeric",
                rows: invalid,
                missing_columns: vec![],
                extra_columns: vec![],
                header_order_matches: None,
                duplicate_headers: vec![],
            });
        }
        if group_by.is_empty() && groups.is_empty() {
            groups.insert(vec![], (0, Money(0)));
        }
        let rows = if let Some(mut disk) = disk {
            let rows = disk.rows()?;
            disk.finish()?;
            if rows.is_empty() && group_by.is_empty() {
                vec![vec!["0".into(), Money(0).decimal()]]
            } else {
                rows
            }
        } else {
            groups
                .into_iter()
                .map(|(mut k, (n, v))| {
                    k.push(n.to_string());
                    k.push(v.decimal());
                    k
                })
                .collect::<Vec<_>>()
        };
        let f = make_fact(
            Operation::Aggregate {
                input,
                group_by: group_by.to_vec(),
                sum: sum.into(),
            },
            t,
            rows.len(),
            0,
            Some(total),
        );
        let mut header = group_by.to_vec();
        header.extend(["count".into(), "sum".into()]);
        self.publish(
            Table {
                header,
                rows,
                sources: t.sources.clone(),
                bom: false,
                generation: u64::MAX,
            },
            f,
        )
    }
    /// Fixed template, only engine-issued facts; no free-form numeric claims or Markdown from input.
    pub fn write_report(&self, facts: &[FactId]) -> Result<String> {
        if facts.len() > self.limits.groups {
            return Err(err("group_limit"));
        }
        let mut out = String::from("# CSV facts\n");
        for id in facts {
            let f = self.fact(*id)?;
            let line = format!(
                "- Fact {}: input rows {}; output rows {}; excluded rows {}; parse error rows {}; sum {}; source IDs {:?}; artifact IDs {:?}\n",
                id.index,
                f.input_rows,
                f.output_rows,
                f.excluded_rows,
                f.parse_error_rows,
                f.sum
                    .as_ref()
                    .map(Money::decimal)
                    .unwrap_or_else(|| "not applicable".into()),
                f.sources,
                f.artifact_refs
            );
            if out.len() + line.len() > self.limits.output_bytes {
                return Err(err("output_limit"));
            }
            self.budget(out.len() + line.len())?;
            out.push_str(&line);
        }
        if out.len() > self.limits.output_bytes {
            return Err(err("output_limit"));
        }
        Ok(out)
    }
    pub fn csv_bytes(&self, id: FileId) -> Result<Vec<u8>> {
        let t = self.table(id)?;
        self.budget(
            table_size(t)
                .checked_mul(2)
                .ok_or_else(|| err("memory_limit"))?,
        )?;
        let mut out = String::new();
        for row in std::iter::once(&t.header).chain(&t.rows) {
            for (i, cell) in row.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                if cell.contains([',', '"', '\n', '\r']) {
                    out.push('"');
                    out.push_str(&cell.replace('"', "\"\""));
                    out.push('"');
                } else {
                    out.push_str(cell);
                }
            }
            out.push('\n');
            if out.len() > self.limits.output_bytes {
                return Err(err("output_limit"));
            }
        }
        Ok(out.into_bytes())
    }
}
fn make_fact(
    operation: Operation,
    t: &Table,
    output_rows: usize,
    excluded_rows: usize,
    sum: Option<Money>,
) -> Fact {
    Fact {
        operation,
        sources: t.sources.clone(),
        input_rows: t.rows.len(),
        output_rows,
        excluded_rows,
        parse_error_rows: 0,
        sum,
        artifact_refs: vec![],
        generation: u64::MAX,
    }
}
fn table_size(t: &Table) -> usize {
    256 + t.sources.len() * 8
        + t.header.iter().map(|s| s.len() + 48).sum::<usize>()
        + t.rows
            .iter()
            .map(|r| 48 + r.iter().map(|s| s.len() + 48).sum::<usize>())
            .sum::<usize>()
}
fn column_index(t: &Table, s: &str) -> Result<usize> {
    t.header
        .iter()
        .position(|x| x == s)
        .ok_or_else(|| err("missing_column"))
}
fn indices(t: &Table, columns: &[String]) -> Result<Vec<usize>> {
    if columns.is_empty()
        || columns.len() > t.header.len()
        || columns.iter().collect::<BTreeSet<_>>().len() != columns.len()
    {
        return Err(err("invalid_columns"));
    }
    columns.iter().map(|s| column_index(t, s)).collect()
}
fn duplicate_headers(header: &[String]) -> Vec<String> {
    let mut seen = BTreeSet::new();
    header
        .iter()
        .filter(|h| !seen.insert((*h).clone()))
        .cloned()
        .collect()
}
fn fact_cost(f: &Fact) -> Option<usize> {
    let mut n = 512usize
        .checked_add(f.sources.len().checked_mul(16)?)?
        .checked_add(f.artifact_refs.len().checked_mul(16)?)?;
    n = n.checked_add(match &f.operation {
        Operation::Inspect { .. } => 32,
        Operation::Concat { inputs } => inputs.len().checked_mul(16)?,
        Operation::Filter { condition, .. } => match condition {
            Condition::Compare { column, value, .. } => 64usize
                .checked_add(column.len())?
                .checked_add(match value {
                    Value::Text(s) => s.len(),
                    Value::Decimal(_) => 32,
                })?,
            Condition::DuplicateKey { columns, .. } => columns
                .iter()
                .try_fold(64usize, |n, s| n.checked_add(s.len()))?,
        },
        Operation::Aggregate { group_by, sum, .. } => group_by
            .iter()
            .try_fold(sum.len().checked_add(64)?, |n, s| n.checked_add(s.len()))?,
        Operation::Report { facts } => facts.len().checked_mul(16)?,
    })?;
    Some(n)
}
fn key(row: &[String], indices: &[usize]) -> Vec<String> {
    indices.iter().map(|i| row[*i].clone()).collect()
}

// Strict four-state parser: unlike permissive CSV readers, quotes after unquoted
// content and bytes after a closing quote are errors, never silently repaired.
fn parse(bytes: &[u8], limits: &Limits) -> Result<Table> {
    let text = std::str::from_utf8(bytes).map_err(|_| err("invalid_utf8"))?;
    let bom = text.starts_with('\u{feff}');
    let text = text.strip_prefix('\u{feff}').unwrap_or(text);
    let mut records = vec![];
    let mut row = vec![];
    let mut cell = String::new();
    let mut state = 0;
    let mut chars = text.chars().peekable();
    let mut pending = false;
    while let Some(c) = chars.next() {
        pending = true;
        if state == 2 {
            if c == '"' {
                state = 3;
            } else {
                cell.push(c);
            }
        } else if state == 3 && c == '"' {
            cell.push('"');
            state = 2;
        } else if c == ',' || c == '\n' || c == '\r' {
            row.push(std::mem::take(&mut cell));
            state = 0;
            if row.len() > limits.columns {
                return Err(err("column_limit"));
            }
            if c != ',' {
                if c == '\r' && chars.next() != Some('\n') {
                    return Err(err("invalid_csv"));
                }
                records.push(std::mem::take(&mut row));
                pending = false;
                if records.len() > limits.rows.saturating_add(1) {
                    return Err(err("row_limit"));
                }
            }
        } else if state == 3 {
            return Err(err("invalid_csv"));
        } else if c == '"' {
            if state != 0 {
                return Err(err("invalid_csv"));
            }
            state = 2;
        } else {
            cell.push(c);
            state = 1;
        }
        if cell.len() > limits.cell_bytes {
            return Err(err("cell_limit"));
        }
    }
    if state == 2 {
        return Err(err("invalid_csv"));
    }
    if pending {
        row.push(cell);
        records.push(row);
    }
    if records.is_empty() {
        return Err(err("missing_header"));
    }
    let header = records.remove(0);
    if header.len() > limits.columns {
        return Err(err("column_limit"));
    }
    if records.len() > limits.rows {
        return Err(err("row_limit"));
    }
    if header.iter().any(String::is_empty)
        || header.iter().collect::<BTreeSet<_>>().len() != header.len()
    {
        return Err(Error {
            code: "invalid_header",
            rows: vec![],
            missing_columns: vec![],
            extra_columns: vec![],
            header_order_matches: None,
            duplicate_headers: duplicate_headers(&header),
        });
    }
    let invalid = records
        .iter()
        .enumerate()
        .filter(|(_, r)| r.len() != header.len())
        .map(|(n, _)| n + 1)
        .collect::<Vec<_>>();
    if !invalid.is_empty() {
        return Err(Error {
            code: "column_mismatch",
            rows: invalid,
            missing_columns: vec![],
            extra_columns: vec![],
            header_order_matches: None,
            duplicate_headers: vec![],
        });
    }
    Ok(Table {
        header,
        rows: records,
        sources: vec![],
        bom,
        generation: u64::MAX,
    })
}
