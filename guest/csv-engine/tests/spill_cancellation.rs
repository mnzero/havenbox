use hb_csv_engine::*;
use std::sync::atomic::Ordering;
#[test]
fn cancel_active_spill_and_preserve_unrelated_file() {
    let path = std::env::temp_dir().join(format!("hb13-cancel-{}", std::process::id()));
    std::fs::create_dir(&path).unwrap();
    let sentinel = path.join("unrelated");
    std::fs::write(&sentinel, b"keep").unwrap();
    let limits = Limits {
        aggregate_memory_bytes: 128,
        ..Limits::default()
    };
    let mut e =
        Engine::new(limits).with_scratch(Scratch::trusted_directory(&path, 2_000_000).unwrap());
    let mut data = String::from("key,amount\n");
    for i in 0..10_000 {
        data.push_str(&format!("k{i},1\n"));
    }
    let id = e.register_input(data.as_bytes()).unwrap();
    let cancel = e.cancellation_handle();
    let worker = std::thread::spawn(move || {
        let result = e.aggregate_csv(id, &["key".into()], "amount");
        (result, e.artifact_count(), e.fact_count())
    });
    std::thread::sleep(std::time::Duration::from_millis(20));
    cancel.store(true, Ordering::Relaxed);
    let (result, artifacts, facts) = worker.join().unwrap();
    assert_eq!(result.unwrap_err().code, "cancelled");
    assert_eq!((artifacts, facts), (1, 0));
    assert_eq!(std::fs::read_dir(&path).unwrap().count(), 1);
    assert_eq!(std::fs::read(&sentinel).unwrap(), b"keep");
    std::fs::remove_file(sentinel).unwrap();
    std::fs::remove_dir(path).unwrap();
}
