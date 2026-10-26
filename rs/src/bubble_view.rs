//! Drawing for the floating pill. Kept separate from the window code so the
//! recorder app can show the same bubble.

use crate::state::State;
use eframe::egui::{self, Align2, Color32, FontId, Pos2, Rect, Rounding, Sense, Stroke, Vec2};

pub const SIZE: Vec2 = Vec2::new(280.0, 44.0);

const BUTTON_R: f32 = 13.0;
const RED: Color32 = Color32::from_rgb(235, 70, 70);

struct Visual {
    label: &'static str,
    dot: Color32,
    pulse: bool,
}

fn visual(state: &State) -> Visual {
    let rgb = |r, g, b| Color32::from_rgb(r, g, b);
    match state {
        State::Loading => Visual { label: "loading", dot: rgb(240, 190, 70), pulse: true },
        State::Idle => Visual { label: "ready", dot: rgb(80, 200, 140), pulse: false },
        State::Listening => Visual { label: "listening", dot: rgb(235, 70, 70), pulse: true },
        State::Transcribing => Visual { label: "transcribing", dot: rgb(90, 150, 240), pulse: true },
        State::Paused => Visual { label: "paused", dot: rgb(140, 140, 150), pulse: false },
        State::Error(_) => Visual { label: "error", dot: rgb(240, 120, 60), pulse: false },
    }
}

/// What the user did to the bubble this frame.
pub struct Output {
    /// The pill body (drag it, or hover it).
    pub body: egui::Response,
    pub record_clicked: bool,
    pub camera_clicked: bool,
}

/// Paints the pill. `recording` and `camera` only change how the two buttons look;
/// the caller decides what they mean.
pub fn draw(ui: &mut egui::Ui, state: &State, recording: bool, camera: bool) -> Output {
    let (rect, body) = ui.allocate_exact_size(SIZE, Sense::click_and_drag());
    let painter = ui.painter_at(rect);
    let radius = Rounding::same(rect.height() / 2.0);

    // Soft shadow: a translucent copy offset down a little.
    let shadow = rect.translate(Vec2::new(0.0, 2.0)).expand(1.0);
    painter.rect_filled(shadow, radius, Color32::from_black_alpha(70));

    // Body. Brightens a little on hover so it reads as clickable.
    let fill = if body.hovered() {
        Color32::from_rgb(38, 38, 46)
    } else {
        Color32::from_rgb(28, 28, 34)
    };
    painter.rect_filled(rect, radius, fill);
    painter.rect_stroke(rect, radius, Stroke::new(1.0_f32, Color32::from_white_alpha(26)));

    // Status dot, with a breathing halo while the app is busy or listening.
    let vis = visual(state);
    let t = ui.input(|i| i.time) as f32;
    let dot_center = Pos2::new(rect.left() + 22.0, rect.center().y);
    if vis.pulse {
        let phase = (t * 3.0).sin() * 0.5 + 0.5; // 0..1
        let halo = 6.0 + 5.0 * phase;
        painter.circle_filled(dot_center, halo, vis.dot.gamma_multiply(0.28 + 0.12 * (1.0 - phase)));
        ui.ctx().request_repaint();
    }
    painter.circle_filled(dot_center, 6.0, vis.dot);

    // Status text. Shows "recording" while the record button is on.
    let label = if recording { "recording" } else { vis.label };
    painter.text(
        Pos2::new(rect.left() + 38.0, rect.center().y),
        Align2::LEFT_CENTER,
        label,
        FontId::proportional(15.0),
        Color32::from_white_alpha(230),
    );

    // Buttons. Allocated after the body so they take the clicks over it.
    let cam_center = Pos2::new(rect.right() - 72.0, rect.center().y);
    let rec_center = Pos2::new(rect.right() - 30.0, rect.center().y);
    let camera_resp = button(ui, "camera", cam_center);
    let record_resp = button(ui, "record", rec_center);

    paint_camera(&painter, cam_center, camera, camera_resp.hovered());
    paint_record(&painter, rec_center, recording, record_resp.hovered());

    Output {
        body,
        record_clicked: record_resp.clicked(),
        camera_clicked: camera_resp.clicked(),
    }
}

fn button(ui: &mut egui::Ui, id: &str, center: Pos2) -> egui::Response {
    let rect = Rect::from_center_size(center, Vec2::splat(BUTTON_R * 2.0 + 4.0));
    ui.interact(rect, ui.id().with(id), Sense::click())
        .on_hover_cursor(egui::CursorIcon::PointingHand)
}

/// Red dot in a ring when idle; a red disc with a white square (stop) when recording.
fn paint_record(painter: &egui::Painter, c: Pos2, recording: bool, hovered: bool) {
    let alpha = if hovered { 255 } else { 220 };
    if recording {
        painter.circle_filled(c, BUTTON_R, RED);
        let s = 6.0;
        painter.rect_filled(
            Rect::from_center_size(c, Vec2::splat(s * 2.0)),
            Rounding::same(1.5),
            Color32::WHITE,
        );
    } else {
        painter.circle_stroke(c, BUTTON_R - 1.0, Stroke::new(1.5_f32, Color32::from_white_alpha(alpha)));
        painter.circle_filled(c, 6.0, RED);
    }
}

/// A small camera glyph: a body with a lens bump. Lit up when the camera is on.
fn paint_camera(painter: &egui::Painter, c: Pos2, on: bool, hovered: bool) {
    let color = if on {
        Color32::from_rgb(80, 200, 140)
    } else {
        Color32::from_white_alpha(if hovered { 240 } else { 200 })
    };
    let body = Rect::from_center_size(c + Vec2::new(0.0, 1.0), Vec2::new(16.0, 11.0));
    painter.rect_stroke(body, Rounding::same(2.5), Stroke::new(1.5_f32, color));
    painter.circle_stroke(c + Vec2::new(0.0, 1.0), 3.0, Stroke::new(1.5_f32, color));
    // Little top bump, like the viewfinder.
    let bump = Rect::from_min_max(
        Pos2::new(c.x - 4.0, c.y - 6.0),
        Pos2::new(c.x + 1.0, c.y - 4.0),
    );
    painter.rect_filled(bump, Rounding::same(1.0), color);
}
