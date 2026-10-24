//! Task-private scratch capability supplied by the trusted caller, never by a tool argument.
use crate::{Error, Money, Result, err};
use std::fs::{File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::{
    Arc,
    atomic::{AtomicBool, AtomicU64, Ordering},
};
static NEXT: AtomicU64 = AtomicU64::new(0);

/// The supervisor must create and protect this directory before constructing the capability.
/// Construction does not certify directory isolation; no untrusted path enters an Operation.
#[derive(Debug)]
pub struct Scratch {
    directory: PathBuf,
    pub(crate) bytes: u64,
}
impl Scratch {
    pub fn trusted_directory(directory: &Path, bytes: u64) -> Result<Self> {
        let meta = std::fs::symlink_metadata(directory).map_err(io_error)?;
        if !meta.is_dir() || meta.file_type().is_symlink() {
            return Err(err("scratch_directory"));
        }
        Ok(Self {
            directory: directory.to_path_buf(),
            bytes,
        })
    }
    pub(crate) fn open(&self, memory: usize, cancelled: Arc<AtomicBool>) -> Result<Spill> {
        let path = self.directory.join(format!(
            "hb-csv-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(&path)
            .map_err(io_error)?;
        // Unlink before processing: the open handle owns storage until close, so
        // errors/cancellation cannot leave named partial data or delete unrelated files.
        std::fs::remove_file(&path).map_err(|_| err("scratch_cleanup"))?;
        Ok(Spill {
            file,
            cancelled,
            bytes: 0,
            limit: self.bytes,
            memory,
            groups: 0,
        })
    }
}
fn io_error(_: std::io::Error) -> Error {
    err("scratch_io")
}
pub(crate) struct Spill {
    file: File,
    cancelled: Arc<AtomicBool>,
    bytes: u64,
    limit: u64,
    memory: usize,
    pub groups: usize,
}
impl Spill {
    // A linear external map intentionally trades CPU for bounded memory. Each record
    // contains a length-prefixed UTF-8 tuple plus fixed count/sum; updates never grow it.
    fn encoded(&self, key: &[String]) -> Result<Vec<u8>> {
        let len = key
            .iter()
            .try_fold(4usize, |n, s| n.checked_add(4 + s.len()))
            .ok_or_else(|| err("memory_limit"))?;
        if len.checked_mul(4).ok_or_else(|| err("memory_limit"))? > self.memory {
            return Err(err("aggregate_memory_limit"));
        }
        let mut b = Vec::with_capacity(len);
        b.extend((key.len() as u32).to_le_bytes());
        for s in key {
            b.extend((s.len() as u32).to_le_bytes());
            b.extend(s.as_bytes());
        }
        Ok(b)
    }
    fn read_record(&mut self) -> Result<Option<(u64, Vec<u8>, u64, Money)>> {
        if self.cancelled.load(Ordering::Relaxed) {
            return Err(err("cancelled"));
        }
        let position = self.file.stream_position().map_err(io_error)?;
        if position == self.bytes {
            return Ok(None);
        }
        let mut n = [0; 4];
        self.file.read_exact(&mut n).map_err(io_error)?;
        let len = u32::from_le_bytes(n) as usize;
        if len * 4 > self.memory {
            return Err(err("aggregate_memory_limit"));
        }
        let mut key = vec![0; len];
        self.file.read_exact(&mut key).map_err(io_error)?;
        let mut count = [0; 8];
        let mut sum = [0; 8];
        self.file.read_exact(&mut count).map_err(io_error)?;
        self.file.read_exact(&mut sum).map_err(io_error)?;
        Ok(Some((
            position,
            key,
            u64::from_le_bytes(count),
            Money(i64::from_le_bytes(sum)),
        )))
    }
    pub fn add(&mut self, key: &[String], value: &Money, max_groups: usize) -> Result<()> {
        let encoded = self.encoded(key)?;
        self.file.rewind().map_err(io_error)?;
        while let Some((pos, k, n, sum)) = self.read_record()? {
            if k == encoded {
                let sum = sum.add(value)?;
                let n = n.checked_add(1).ok_or_else(|| err("overflow"))?;
                self.file
                    .seek(SeekFrom::Start(pos + 4 + k.len() as u64))
                    .map_err(io_error)?;
                self.file.write_all(&n.to_le_bytes()).map_err(io_error)?;
                self.file
                    .write_all(&sum.0.to_le_bytes())
                    .map_err(io_error)?;
                return Ok(());
            }
        }
        if self.groups >= max_groups {
            return Err(err("group_limit"));
        }
        let bytes = 20 + encoded.len() as u64;
        if bytes > self.limit.saturating_sub(self.bytes) {
            return Err(err("scratch_disk_limit"));
        }
        self.file.seek(SeekFrom::End(0)).map_err(io_error)?;
        self.file
            .write_all(&(encoded.len() as u32).to_le_bytes())
            .map_err(io_error)?;
        self.file.write_all(&encoded).map_err(io_error)?;
        self.file.write_all(&1u64.to_le_bytes()).map_err(io_error)?;
        self.file
            .write_all(&value.0.to_le_bytes())
            .map_err(io_error)?;
        self.bytes += bytes;
        self.groups += 1;
        Ok(())
    }
    pub fn count(&mut self, key: &[String]) -> Result<u64> {
        let encoded = self.encoded(key)?;
        self.file.rewind().map_err(io_error)?;
        while let Some((_, k, n, _)) = self.read_record()? {
            if k == encoded {
                return Ok(n);
            }
        }
        Ok(0)
    }
    pub fn rows(&mut self) -> Result<Vec<Vec<String>>> {
        self.file.rewind().map_err(io_error)?;
        let mut rows = vec![];
        while let Some((_, k, n, sum)) = self.read_record()? {
            let mut cursor = &k[..];
            let mut word = [0; 4];
            cursor.read_exact(&mut word).map_err(io_error)?;
            let mut row = vec![];
            for _ in 0..u32::from_le_bytes(word) {
                cursor.read_exact(&mut word).map_err(io_error)?;
                let len = u32::from_le_bytes(word) as usize;
                if len > cursor.len() {
                    return Err(err("scratch_corrupt"));
                }
                row.push(
                    std::str::from_utf8(&cursor[..len])
                        .map_err(|_| err("scratch_corrupt"))?
                        .to_string(),
                );
                cursor = &cursor[len..];
            }
            row.push(n.to_string());
            row.push(sum.decimal());
            rows.push(row);
        }
        rows.sort_by(|a, b| a[..a.len() - 2].cmp(&b[..b.len() - 2]));
        Ok(rows)
    }
    /// Close the already-unlinked temporary handle before any result is published.
    pub fn finish(self) -> Result<()> {
        self.file.sync_all().map_err(io_error)
    }
}
