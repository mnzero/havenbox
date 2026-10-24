fn main() -> std::process::ExitCode {
    let args: Vec<_> = std::env::args().skip(1).collect();
    if args == ["--version"] {
        println!(
            "hb-supervisor {} · hb.channel.v{}",
            env!("CARGO_PKG_VERSION"),
            hb_protocol::VERSION
        );
        return std::process::ExitCode::SUCCESS;
    }
    if args == ["--inspect-boot"] {
        return match hb_supervisor::inspect_live_boot() {
            Ok(()) => {
                println!("HB-11 boot topology accepted; task admission remains closed");
                std::process::ExitCode::SUCCESS
            }
            Err(error) => {
                eprintln!("HB-11 boot rejected: {error:?}; no process started");
                std::process::ExitCode::from(78)
            }
        };
    }
    eprintln!(
        "Product supervision is not implemented: HB-11 candidate lacks validated image/transport/launchd integration; no process started."
    );
    std::process::ExitCode::from(78)
}
