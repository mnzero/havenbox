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
    eprintln!("Guest supervision is not implemented (HB-11); no process started.");
    std::process::ExitCode::from(78)
}
