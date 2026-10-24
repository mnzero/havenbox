//! Packaging boundary only; no native harness protocols are implemented here.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Harness {
    Builtin,
    KimiCli,
    Pi,
    Dsh,
}
#[derive(Debug, PartialEq, Eq)]
pub struct AdapterUnavailable {
    pub harness: Harness,
}

/// Fail closed until the actual versioned adapter and sandbox have been integrated.
pub fn probe(harness: Harness) -> Result<(), AdapterUnavailable> {
    Err(AdapterUnavailable { harness })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn no_candidate_harness_claims_availability() {
        for harness in [
            Harness::Builtin,
            Harness::KimiCli,
            Harness::Pi,
            Harness::Dsh,
        ] {
            assert_eq!(probe(harness), Err(AdapterUnavailable { harness }));
        }
    }
}
