#![cfg_attr(all(windows, feature = "gpui-ui"), windows_subsystem = "windows")]

#[cfg(feature = "gpui-ui")]
mod workspace;

#[cfg(all(feature = "gpui-ui", not(feature = "mock-ui")))]
mod backend;

#[cfg(all(feature = "gpui-ui", feature = "mock-ui"))]
fn main() {
    use std::sync::Arc;

    use dcmget_ui_kit::RecordingCommandSink;

    workspace::run(Arc::new(RecordingCommandSink::default()));
}

#[cfg(all(feature = "gpui-ui", not(feature = "mock-ui")))]
fn main() {
    match backend::smoke_options(std::env::args_os()) {
        Ok(Some(options)) => std::process::exit(backend::run_backend_smoke(&options)),
        Ok(None) => workspace::run(backend::open()),
        Err(error) => {
            eprintln!("DcmGet 后台 smoke 参数错误：{error}");
            std::process::exit(2);
        }
    }
}

#[cfg(not(feature = "gpui-ui"))]
fn main() {
    println!("DcmGet desktop requires the gpui-ui feature.");
}
