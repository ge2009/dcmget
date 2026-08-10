use clap::Parser;

#[derive(Debug, Parser)]
#[command(
    name = "dcmget-updater",
    version,
    about = "DcmGet signed update helper"
)]
struct Arguments {
    /// Verify updater wiring without replacing an installed application.
    #[arg(long)]
    readiness: bool,
}

fn main() {
    let arguments = Arguments::parse();
    if arguments.readiness {
        println!(
            "DcmGet updater: replacement is disabled until signed-manifest compatibility tests pass"
        );
        return;
    }
    eprintln!("DcmGet updater is not available in this technical preview");
    std::process::exit(2);
}
