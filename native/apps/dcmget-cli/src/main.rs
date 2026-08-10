mod download;

use std::path::PathBuf;
use std::process::ExitCode;

use anyhow::Context;
use clap::error::ErrorKind;
use clap::{Parser, Subcommand};
use dcmget_state::LegacyConfig;

use crate::download::DownloadOutcome;

#[derive(Debug, Parser)]
#[command(name = "dcmget-cli", version, about = "DcmGet native migration CLI")]
struct Arguments {
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Subcommand)]
enum Command {
    /// Validate and print the normalized legacy configuration without changing it.
    ValidateConfig { config: PathBuf },
    /// Print the native migration readiness state.
    Readiness,
    /// Download studies with native Study Root C-MOVE and Storage SCP.
    Download {
        /// Legacy-compatible JSON configuration.
        #[arg(long)]
        config: PathBuf,
        /// UTF-8 text file containing one Accession Number per line.
        #[arg(long)]
        accessions: PathBuf,
        /// Override the configured direct-to-disk destination root.
        #[arg(long)]
        destination: Option<PathBuf>,
    },
}

#[tokio::main]
async fn main() -> ExitCode {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "dcmget=info".into()),
        )
        .init();

    let arguments = match Arguments::try_parse() {
        Ok(arguments) => arguments,
        Err(error) => {
            let code = u8::from(!matches!(
                error.kind(),
                ErrorKind::DisplayHelp | ErrorKind::DisplayVersion
            ));
            let _ = error.print();
            return ExitCode::from(code);
        }
    };

    match execute(arguments.command).await {
        Ok(code) => ExitCode::from(code),
        Err(error) => {
            eprintln!("error: {error:#}");
            ExitCode::from(1)
        }
    }
}

async fn execute(command: Command) -> anyhow::Result<u8> {
    match command {
        Command::ValidateConfig { config } => {
            let raw = tokio::fs::read_to_string(&config)
                .await
                .with_context(|| format!("failed to read {}", config.display()))?;
            let parsed = LegacyConfig::from_json(&raw)?;
            println!("{}", serde_json::to_string_pretty(&parsed)?);
            Ok(0)
        }
        Command::Readiness => {
            println!(
                "DcmGet native CLI download is available for transfer syntaxes supported by the pinned dicom-rs registry; unsupported or unknown transfer syntaxes are not claimed. PDI, licensing, and registration are outside this CLI path."
            );
            Ok(0)
        }
        Command::Download {
            config,
            accessions,
            destination,
        } => Ok(
            match download::run(&config, &accessions, destination).await? {
                DownloadOutcome::Success => 0,
                DownloadOutcome::Failed => 2,
                DownloadOutcome::Interrupted => 130,
            },
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn download_arguments_match_the_documented_contract() {
        let arguments = Arguments::try_parse_from([
            "dcmget-cli",
            "download",
            "--config",
            "config.json",
            "--accessions",
            "access.txt",
            "--destination",
            "Dicom",
        ])
        .expect("download arguments should parse");
        let Command::Download {
            config,
            accessions,
            destination,
        } = arguments.command
        else {
            panic!("expected download command");
        };
        assert_eq!(config, PathBuf::from("config.json"));
        assert_eq!(accessions, PathBuf::from("access.txt"));
        assert_eq!(destination, Some(PathBuf::from("Dicom")));
    }

    #[test]
    fn malformed_cli_input_is_classified_as_exit_one() {
        let error = Arguments::try_parse_from(["dcmget-cli", "download"])
            .expect_err("required download inputs must not be optional");
        assert!(!matches!(
            error.kind(),
            ErrorKind::DisplayHelp | ErrorKind::DisplayVersion
        ));
    }
}
