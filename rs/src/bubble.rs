//! The floating bubble window: borderless, transparent, always on top, and it never
//! takes keyboard focus, so dictation goes into whichever app the user was typing in.

use crate::bubble_view;
use crate::camera::{self, CameraFeed};
use crate::config::Config;
use crate::recorder::Recorder;
use crate::state::State;
use eframe::egui;

const WINDOW_TITLE: &str = "dictate-bubble";

pub fn run(cfg: Config) -> anyhow::Result<()> {
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_title(WINDOW_TITLE)
            .with_inner_size(bubble_view::SIZE)
            .with_decorations(false)
            .with_transparent(true)
            .with_always_on_top()
            .with_taskbar(false),
        ..Default::default()
    };
    eframe::run_native(
        "Dictate",
        options,
        Box::new(move |_cc| Ok(Box::new(Bubble::new(cfg)))),
    )
    .map_err(|e| anyhow::anyhow!("{e}"))
}

const CAMERA_TITLE: &str = "dictate-camera";

/// `--camera-only` mode: just the round camera preview, for the Python dictation
/// bubble's camera button. It exits when stdin closes, i.e. when the bubble turns the
/// camera off or quits.
pub fn run_camera_only(cfg: Config) -> anyhow::Result<()> {
    let feed = CameraFeed::start(&cfg)?;
    std::thread::spawn(|| {
        let mut line = String::new();
        while std::io::stdin().read_line(&mut line).map(|n| n > 0).unwrap_or(false) {
            line.clear();
        }
        std::process::exit(0);
    });

    let side = camera::PREVIEW_SIDE;
    let mut viewport = egui::ViewportBuilder::default()
        .with_title(CAMERA_TITLE)
        .with_inner_size([side, side])
        .with_decorations(false)
        .with_transparent(true)
        .with_always_on_top()
        .with_taskbar(false);
    #[cfg(windows)]
    if let Some(pos) = win32::bottom_left(side) {
        viewport = viewport.with_position(pos);
    }
    eframe::run_native(
        "Dictate camera",
        eframe::NativeOptions { viewport, ..Default::default() },
        Box::new(move |_cc| {
            Ok(Box::new(CameraOnly {
                feed,
                styled: false,
                side: camera::PREVIEW_SIDE,
                clicks: Vec::new(),
                resizing: false,
            }))
        }),
    )
    .map_err(|e| anyhow::anyhow!("{e}"))
}

/// Three clicks this close together (seconds) count as a triple-click.
const TRIPLE_CLICK_WINDOW: f64 = 1.5;
const MIN_SIDE: f32 = 120.0;
const MAX_SIDE: f32 = 480.0;

struct CameraOnly {
    feed: CameraFeed,
    styled: bool,
    /// Current size of the circle in points; the window follows it.
    side: f32,
    /// Times of recent clicks, for spotting a triple-click.
    clicks: Vec<f64>,
    /// Set by a triple-click: the mouse wheel then changes the size, with a white ring around the circle.
    resizing: bool,
}

impl eframe::App for CameraOnly {
    fn clear_color(&self, _visuals: &egui::Visuals) -> [f32; 4] {
        [0.0, 0.0, 0.0, 0.0]
    }

    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        if !self.styled {
            self.styled = true;
            #[cfg(windows)]
            win32::make_no_activate(CAMERA_TITLE);
        }
        let now = ctx.input(|i| i.time);
        egui::CentralPanel::default()
            .frame(egui::Frame::none())
            .show(ctx, |ui| {
                let (rect, response) =
                    ui.allocate_exact_size(egui::Vec2::splat(self.side), egui::Sense::click_and_drag());
                if response.drag_started() {
                    ctx.send_viewport_cmd(egui::ViewportCommand::StartDrag);
                }
                if response.hovered() {
                    ctx.set_cursor_icon(egui::CursorIcon::Grab);
                }
                if response.clicked() {
                    self.clicks.retain(|t| now - t < TRIPLE_CLICK_WINDOW);
                    self.clicks.push(now);
                    if self.clicks.len() >= 3 {
                        self.clicks.clear();
                        self.resizing = !self.resizing;
                    }
                }
                if self.resizing {
                    let scroll = ui.input(|i| i.smooth_scroll_delta.y);
                    if scroll != 0.0 {
                        self.side = (self.side + scroll * 0.5).clamp(MIN_SIDE, MAX_SIDE);
                        ctx.send_viewport_cmd(egui::ViewportCommand::InnerSize(egui::vec2(self.side, self.side)));
                    }
                }
                camera::paint_preview(ui, &self.feed.shared(), rect);
                if self.resizing {
                    ui.painter().circle_stroke(
                        rect.center(),
                        rect.width() / 2.0 - 2.0,
                        egui::Stroke::new(3.0, egui::Color32::WHITE),
                    );
                }
            });
    }
}

fn preview_state() -> Option<State> {
    match std::env::var("DICTATE_PREVIEW").ok()?.as_str() {
        "idle" => Some(State::Idle),
        "listening" => Some(State::Listening),
        "transcribing" => Some(State::Transcribing),
        "paused" => Some(State::Paused),
        "error" => Some(State::Error("preview".into())),
        _ => None,
    }
}

struct Bubble {
    state: State,
    #[allow(dead_code)] // used from phase 2 onward (hide timer, paste mode)
    cfg: Config,
    no_activate_applied: bool,
    /// Set while the record button is on.
    recorder: Option<Recorder>,
    /// Set while the camera button is on; the preview circle is drawn from it.
    camera: Option<CameraFeed>,
    /// Current window height, so the size is only sent when it changes.
    window_h: f32,
}

/// Space between the pill and the camera circle.
const GAP: f32 = 8.0;

impl Bubble {
    fn toggle_camera(&mut self) {
        if self.camera.take().is_none() {
            match CameraFeed::start(&self.cfg) {
                Ok(feed) => self.camera = Some(feed),
                Err(e) => eprintln!("can't start camera: {e:#}"),
            }
        }
    }

    fn toggle_recording(&mut self) {
        match self.recorder.take() {
            Some(rec) => match rec.stop() {
                Ok(path) => eprintln!("saved {}", path.display()),
                Err(e) => eprintln!("recording failed to save: {e:#}"),
            },
            None => match Recorder::start(&self.cfg) {
                Ok(rec) => self.recorder = Some(rec),
                Err(e) => eprintln!("can't start recording: {e:#}"),
            },
        }
    }
}

impl Bubble {
    fn new(cfg: Config) -> Self {
        // Test switch: DICTATE_CAMERA=1 turns the camera on at launch.
        let camera = std::env::var_os("DICTATE_CAMERA")
            .and_then(|_| CameraFeed::start(&cfg).map_err(|e| eprintln!("camera: {e:#}")).ok());
        Self {
            // Phase 1 has no model yet, so the bubble starts in Loading.
            // DICTATE_PREVIEW=idle|listening|transcribing|paused|error shows a state for visual checks.
            state: preview_state().unwrap_or(State::Loading),
            cfg,
            no_activate_applied: false,
            recorder: None,
            camera,
            window_h: bubble_view::SIZE.y,
        }
    }
}

impl eframe::App for Bubble {
    fn clear_color(&self, _visuals: &egui::Visuals) -> [f32; 4] {
        [0.0, 0.0, 0.0, 0.0] // fully transparent window background
    }

    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        if !self.no_activate_applied {
            self.no_activate_applied = true;
            #[cfg(windows)]
            win32::make_no_activate(WINDOW_TITLE);
        }

        // The window grows to make room for the camera circle under the pill.
        let want_h = if self.camera.is_some() {
            bubble_view::SIZE.y + GAP + camera::PREVIEW_SIDE
        } else {
            bubble_view::SIZE.y
        };
        if (want_h - self.window_h).abs() > 0.5 {
            self.window_h = want_h;
            ctx.send_viewport_cmd(egui::ViewportCommand::InnerSize(egui::vec2(
                bubble_view::SIZE.x,
                want_h,
            )));
        }

        egui::CentralPanel::default()
            .frame(egui::Frame::none())
            .show(ctx, |ui| {
                let out = bubble_view::draw(ui, &self.state, self.recorder.is_some(), self.camera.is_some());
                if out.record_clicked {
                    self.toggle_recording();
                }
                if out.camera_clicked {
                    self.toggle_camera();
                }
                if out.body.drag_started() {
                    ctx.send_viewport_cmd(egui::ViewportCommand::StartDrag);
                }
                if out.body.hovered() {
                    ctx.set_cursor_icon(egui::CursorIcon::Grab);
                }

                if let Some(feed) = &self.camera {
                    ui.add_space(GAP);
                    let (row, response) = ui.allocate_exact_size(
                        egui::vec2(bubble_view::SIZE.x, camera::PREVIEW_SIDE),
                        egui::Sense::drag(),
                    );
                    if response.drag_started() {
                        ctx.send_viewport_cmd(egui::ViewportCommand::StartDrag);
                    }
                    let circle = egui::Rect::from_center_size(row.center(), egui::Vec2::splat(camera::PREVIEW_SIDE));
                    camera::paint_preview(ui, &feed.shared(), circle);
                }
            });
    }
}

#[cfg(windows)]
pub mod win32 {
    use eframe::egui;
    use windows::core::HSTRING;
    use windows::Win32::Foundation::HWND;
    use windows::Win32::UI::HiDpi::GetDpiForSystem;
    use windows::Win32::UI::WindowsAndMessaging::{
        FindWindowW, GetSystemMetrics, GetWindowLongPtrW, SetWindowLongPtrW, GWL_EXSTYLE, SM_CYSCREEN,
        WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW,
    };

    /// Top-left corner (in points) that puts a `side`-sized window at the bottom-left
    /// of the primary screen, clear of the taskbar.
    pub fn bottom_left(side: f32) -> Option<egui::Pos2> {
        let (h, dpi) = unsafe { (GetSystemMetrics(SM_CYSCREEN) as f32, GetDpiForSystem() as f32) };
        if h <= 0.0 || dpi <= 0.0 {
            return None;
        }
        let scale = dpi / 96.0;
        Some(egui::pos2(24.0, h / scale - side - 72.0))
    }

    /// Mark the bubble so clicking it doesn't steal focus from the app the user is
    /// dictating into, and so it stays out of Alt-Tab.
    pub fn make_no_activate(title: &str) {
        unsafe {
            let Ok(hwnd) = FindWindowW(None, &HSTRING::from(title)) else {
                return;
            };
            if hwnd == HWND::default() {
                return;
            }
            let ex = GetWindowLongPtrW(hwnd, GWL_EXSTYLE);
            SetWindowLongPtrW(
                hwnd,
                GWL_EXSTYLE,
                ex | WS_EX_NOACTIVATE.0 as isize | WS_EX_TOOLWINDOW.0 as isize,
            );
        }
    }
}
