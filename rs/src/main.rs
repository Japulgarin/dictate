//! Dictate (Rust): screen recorder, camera bubble and studio export, plus the
//! standalone bubble window.
//!
//! The Python dictation bubble drives this exe in helper modes:
//!   dictate.exe --record [--out DIR] [--config FILE]
//!       Records screen + mic + cursor until a line `stop` (or EOF) arrives on stdin.
//!       Prints `recording`, `saved <mp4>`, `rendering`, `studio <mp4>` or `error <msg>`,
//!       one per line, as things happen.
//!   dictate.exe --camera-only [--config FILE]
//!       Shows only the round camera preview; exits when stdin closes.
//!   dictate.exe --studio <mp4>
//!       Re-renders the studio version of an existing recording.
//! With no mode flag it opens the standalone Rust bubble.

mod bubble;
mod bubble_view;
mod camera;
mod config;
mod cursor;
mod recorder;
mod state;
mod studio;

use std::io::{BufRead, Write};
use std::path::PathBuf;

#[cfg(windows)]
mod singleton {
    use windows::core::w;
    use windows::Win32::Foundation::{GetLastError, ERROR_ALREADY_EXISTS};
    use windows::Win32::System::Threading::CreateMutexW;

    /// Returns `true` if this is the only running instance. The handle is not closed,
    /// so the named mutex lives until the process exits.
    pub fn acquire() -> bool {
        unsafe {
            match CreateMutexW(None, true, w!("Local\\dictate-rs-singleton")) {
                Ok(_handle) => GetLastError() != ERROR_ALREADY_EXISTS,
                Err(_) => true, // couldn't create the mutex: don't block startup
            }
        }
    }
}

#[cfg(not(windows))]
mod singleton {
    pub fn acquire() -> bool {
        true
    }
}

fn main() -> anyhow::Result<()> {
    // Cursor coordinates must be physical pixels to line up with the screen recording.
    #[cfg(windows)]
    unsafe {
        use windows::Win32::UI::HiDpi::{SetProcessDpiAwarenessContext, DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2};
        let _ = SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    }

    let args: Vec<String> = std::env::args().collect();
    let flag = |name: &str| args.iter().any(|a| a == name);
    let value = |name: &str| {
        args.iter()
            .position(|a| a == name)
            .and_then(|i| args.get(i + 1))
            .cloned()
    };

    let path = value("--config").map(PathBuf::from).unwrap_or_else(config::default_path);
    let mut cfg = match config::Config::load(&path) {
        Ok(c) => c,
        Err(e) => {
            // Surface the error rather than running with wrong settings.
            say(&format!("error config error in {}: {e:#}", path.display()));
            return Err(e);
        }
    };
    if let Some(dir) = value("--out") {
        cfg.recordings_dir = dir;
    }
    if let Some(region) = value("--region") {
        cfg.record_region = region; // "full", "left" or "right"
    }

    if flag("--record") {
        if let Err(e) = record_until_stop(&cfg) {
            say(&format!("error {e:#}"));
            std::process::exit(1);
        }
        return Ok(());
    }
    if flag("--camera-only") {
        return bubble::run_camera_only(cfg);
    }
    if let Some(video) = value("--studio") {
        let out = studio::render(&cfg.ffmpeg, &cfg.record_encoder, cfg.record_fps, video.as_ref())?;
        say(&format!("studio {}", out.display()));
        return Ok(());
    }
    // Headless check: `dictate.exe --record-seconds 5` records for 5 s and exits.
    if let Some(secs) = value("--record-seconds") {
        let secs: u64 = secs.parse().unwrap_or(5);
        let rec = recorder::Recorder::start(&cfg)?;
        std::thread::sleep(std::time::Duration::from_secs(secs));
        say(&format!("saved {}", rec.stop()?.display()));
        return Ok(());
    }

    if !singleton::acquire() {
        eprintln!("dictate is already running");
        return Ok(());
    }
    bubble::run(cfg)
}

/// `--record` mode: record until the parent says `stop` (or goes away), then save and,
/// if enabled, render the studio version.
fn record_until_stop(cfg: &config::Config) -> anyhow::Result<()> {
    let rec = recorder::Recorder::start(cfg)?;
    say("recording");

    // EOF also stops, so a crashed parent still leaves a saved recording.
    let stdin = std::io::stdin();
    for line in stdin.lock().lines() {
        match line {
            Ok(l) if l.trim() == "stop" => break,
            Ok(_) => continue,
            Err(_) => break,
        }
    }

    let video = rec.stop()?;
    say(&format!("saved {}", video.display()));
    if cfg.record_studio {
        say("rendering");
        match studio::render(&cfg.ffmpeg, &cfg.record_encoder, cfg.record_fps, &video) {
            Ok(out) => say(&format!("studio {}", out.display())),
            Err(e) => say(&format!("error studio export failed: {e:#}")),
        }
    }
    Ok(())
}

/// One status line for the parent process, flushed right away. Write errors are
/// ignored (unlike `println!`), so a parent that already quit doesn't abort the save.
fn say(msg: &str) {
    let mut out = std::io::stdout();
    let _ = writeln!(out, "{msg}");
    let _ = out.flush();
}
