//! Cursor telemetry: while recording, samples the mouse position and the left button
//! into a CSV next to the video. The editor reads it to zoom toward clicks and to
//! smooth the cursor path. Coordinates are physical screen pixels, the same space
//! that `gdigrab` records.

use anyhow::Result;
use std::fs::File;
use std::io::{BufWriter, Write};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};
use windows::Win32::Foundation::POINT;
use windows::Win32::UI::Input::KeyboardAndMouse::{GetAsyncKeyState, VK_LBUTTON};
use windows::Win32::UI::WindowsAndMessaging::{GetCursorPos, GetSystemMetrics, SM_CXSCREEN, SM_CYSCREEN};

/// Sample rate: about 60 Hz, enough for smooth zoom paths.
const PERIOD: Duration = Duration::from_millis(16);

pub struct CursorTrack {
    stop: Arc<AtomicBool>,
    handle: Option<JoinHandle<()>>,
}

impl CursorTrack {
    /// Starts sampling into `path`. The first line is `# screen W H`, then the header
    /// `t_ms,x,y,left` and one row per sample.
    pub fn start(path: &Path) -> Result<Self> {
        let mut out = BufWriter::new(File::create(path)?);
        let (w, h) = unsafe { (GetSystemMetrics(SM_CXSCREEN), GetSystemMetrics(SM_CYSCREEN)) };
        writeln!(out, "# screen {w} {h}")?;
        writeln!(out, "t_ms,x,y,left")?;

        let stop = Arc::new(AtomicBool::new(false));
        let flag = Arc::clone(&stop);
        let handle = thread::spawn(move || {
            let t0 = Instant::now();
            while !flag.load(Ordering::Relaxed) {
                let mut p = POINT::default();
                if unsafe { GetCursorPos(&mut p) }.is_ok() {
                    // High bit set = the button is down right now.
                    let down = (unsafe { GetAsyncKeyState(VK_LBUTTON.0 as i32) } as u16 & 0x8000) != 0;
                    let _ = writeln!(out, "{},{},{},{}", t0.elapsed().as_millis(), p.x, p.y, down as u8);
                }
                thread::sleep(PERIOD);
            }
            let _ = out.flush();
        });

        Ok(Self { stop, handle: Some(handle) })
    }

    /// Stops sampling and waits until the file is fully written.
    pub fn stop(mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(h) = self.handle.take() {
            let _ = h.join();
        }
    }
}
