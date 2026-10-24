//! Process-isolated allocator regression for the filter parameter preflight.
//! The input and 1 MiB condition are built before the observation window.
use hb_csv_engine::{Compare, Condition, Engine, Limits, Value};
use std::alloc::{GlobalAlloc, Layout, System};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

struct TrackingAllocator;
static OBSERVE: AtomicBool = AtomicBool::new(false);
static MAX_SINGLE_ALLOCATION: AtomicUsize = AtomicUsize::new(0);

unsafe impl GlobalAlloc for TrackingAllocator {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        record(layout.size());
        // SAFETY: forwarding the layout and request to the system allocator.
        unsafe { System.alloc(layout) }
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        // SAFETY: forwarding the original allocation to the system allocator.
        unsafe { System.dealloc(ptr, layout) }
    }

    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
        record(new_size);
        // SAFETY: forwarding the allocation and sizes to the system allocator.
        unsafe { System.realloc(ptr, layout, new_size) }
    }
}

#[global_allocator]
static ALLOCATOR: TrackingAllocator = TrackingAllocator;

#[inline]
fn record(size: usize) {
    if OBSERVE.load(Ordering::Relaxed) {
        // This path performs no formatting or allocation, so tracking cannot recurse.
        MAX_SINGLE_ALLOCATION.fetch_max(size, Ordering::Relaxed);
    }
}

#[test]
fn filter_rejects_large_text_without_over_budget_allocation() {
    let mut limits = Limits::default();
    limits.memory_bytes = 8192;
    let mut engine = Engine::new(limits);
    let input = engine.register_input(b"amount\n1\n").unwrap();
    // Construct caller-owned input before observing the engine operation.
    let condition = Condition::Compare {
        column: "amount".to_owned(),
        op: Compare::Eq,
        value: Value::Text("x".repeat(1024 * 1024)),
    };
    let before = (engine.artifact_count(), engine.fact_count());

    MAX_SINGLE_ALLOCATION.store(0, Ordering::Relaxed);
    OBSERVE.store(true, Ordering::Relaxed);
    let result = engine.filter_rows(input, condition);
    OBSERVE.store(false, Ordering::Relaxed);

    assert_eq!(result.unwrap_err().code, "memory_limit");
    assert!(
        MAX_SINGLE_ALLOCATION.load(Ordering::Relaxed) <= 8192,
        "filter allocated more than its budget"
    );
    assert_eq!(before, (engine.artifact_count(), engine.fact_count()));
}
