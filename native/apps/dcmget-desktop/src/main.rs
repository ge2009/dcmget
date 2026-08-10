#[cfg(feature = "gpui-ui")]
mod workspace;

#[cfg(feature = "gpui-ui")]
fn main() {
    workspace::run();
}

#[cfg(not(feature = "gpui-ui"))]
fn main() {
    use dcmget_ui_kit::WorkspaceSnapshot;

    let snapshot = WorkspaceSnapshot::technical_gate_sample();
    println!(
        "DcmGet 4 GPUI technical gate (headless): {} profiles, {} active task(s)",
        snapshot.profiles.len(),
        snapshot.tasks.len()
    );
    println!("Run with --no-default-features --features gpui-ui to open the native shell.");
}
