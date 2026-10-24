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
    if args.len() == 5 && args[0] == "--run-synthetic" {
        let instance = match parse_instance(&args[1]) {
            Some(value) => value,
            None => return unavailable("invalid instance"),
        };
        let epoch = match args[2].parse::<u64>() {
            Ok(value) if value > 0 => value,
            _ => return unavailable("invalid epoch"),
        };
        let role = match args[3].as_str() {
            "harness" => hb_supervisor::Role::Harness,
            "worker" => hb_supervisor::Role::Worker,
            _ => return unavailable("invalid synthetic role"),
        };
        let test_case = match args[4].as_str() {
            "normal" => hb_supervisor::SyntheticCase::Normal,
            "output-overflow" => hb_supervisor::SyntheticCase::OutputOverflow,
            "hold" => hb_supervisor::SyntheticCase::Hold,
            _ => return unavailable("invalid synthetic case"),
        };
        return match hb_supervisor::runtime::run(instance, epoch, role, test_case) {
            Ok(()) => std::process::ExitCode::SUCCESS,
            Err(error) => unavailable(&format!("runtime failed: {error:?}")),
        };
    }
    eprintln!(
        "Product supervision is not implemented: HB-11 candidate lacks validated image/transport/launchd integration; no process started."
    );
    std::process::ExitCode::from(78)
}

fn parse_instance(value: &str) -> Option<[u8; 16]> {
    if value.len() != 32 || !value.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return None;
    }
    let mut result = [0; 16];
    for (index, byte) in result.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&value[index * 2..index * 2 + 2], 16).ok()?;
    }
    if result == [0; 16] {
        None
    } else {
        Some(result)
    }
}

fn unavailable(message: &str) -> std::process::ExitCode {
    eprintln!("HB-11 {message}; no execution remains active");
    std::process::ExitCode::from(78)
}
