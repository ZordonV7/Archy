use tauri::{
    menu::{Menu, MenuItem},
    tray::TrayIconBuilder,
    Manager, WindowEvent,
};

/// Commands callable from the frontend via `invoke()`

#[tauri::command]
async fn toggle_dashboard(app: tauri::AppHandle) -> Result<bool, String> {
    if let Some(window) = app.get_webview_window("dashboard") {
        if window.is_visible().unwrap_or(false) {
            window.hide().map_err(|e| e.to_string())?;
            Ok(false)
        } else {
            window.show().map_err(|e| e.to_string())?;
            window.set_focus().map_err(|e| e.to_string())?;
            Ok(true)
        }
    } else {
        Err("Dashboard window not found".into())
    }
}

#[tauri::command]
async fn show_dashboard(app: tauri::AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("dashboard") {
        window.show().map_err(|e| e.to_string())?;
        window.set_focus().map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
async fn hide_dashboard(app: tauri::AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("dashboard") {
        window.hide().map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
async fn minimize_to_tray(app: tauri::AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("mascot") {
        window.hide().map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
async fn show_mascot(app: tauri::AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("mascot") {
        window.show().map_err(|e| e.to_string())?;
    }
    Ok(())
}

/// Show the speech bubble window
#[tauri::command]
async fn show_bubble(app: tauri::AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("bubble") {
        window.show().map_err(|e| e.to_string())?;
    }
    Ok(())
}

/// Hide the speech bubble window
#[tauri::command]
async fn hide_bubble(app: tauri::AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("bubble") {
        window.hide().map_err(|e| e.to_string())?;
    }
    Ok(())
}

/// Position the bubble window relative to the mascot window
#[tauri::command]
async fn position_bubble(app: tauri::AppHandle, x: i32, y: i32) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("bubble") {
        use tauri::PhysicalPosition;
        window.set_position(PhysicalPosition { x, y }).map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
async fn quit_app(app: tauri::AppHandle) -> Result<(), String> {
    app.exit(0);
    Ok(())
}

#[tauri::command]
fn is_tauri() -> bool {
    true
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            // Build system tray — just 2 options: Show Dashboard + Exit
            let dashboard_item = MenuItem::with_id(app, "dashboard", "Show Dashboard", true, None::<&str>)?;
            let quit_item = MenuItem::with_id(app, "quit", "Exit", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&dashboard_item, &quit_item])?;

            // Use default window icon for tray (optional — app works without it)
            if let Some(icon) = app.default_window_icon().cloned() {
                let _tray = TrayIconBuilder::new()
                    .icon(icon)
                    .menu(&menu)
                    .tooltip("Archy — Your adorable scheduling companion")
                    .on_menu_event(|app, event| match event.id.as_ref() {
                        "dashboard" => {
                            if let Some(window) = app.get_webview_window("dashboard") {
                                let _ = window.show();
                                let _ = window.set_focus();
                            }
                        }
                        "quit" => {
                            app.exit(0);
                        }
                        _ => {}
                    })
                    .on_tray_icon_event(|tray, event| {
                        // Left-click tray icon → show mascot
                        if let tauri::tray::TrayIconEvent::Click {
                            button: tauri::tray::MouseButton::Left,
                            ..
                        } = event
                        {
                            let app = tray.app_handle();
                            if let Some(window) = app.get_webview_window("mascot") {
                                let _ = window.show();
                            }
                        }
                    })
                    .build(app);
                // tray is intentionally dropped — kept alive by Tauri
            }

            // Hide mascot to tray on close (don't quit)
            if let Some(mascot_window) = app.get_webview_window("mascot") {
                let h = app.handle().clone();
                mascot_window.on_window_event(move |event| {
                    if let WindowEvent::CloseRequested { api, .. } = event {
                        api.prevent_close();
                        if let Some(w) = h.get_webview_window("mascot") {
                            let _ = w.hide();
                        }
                    }
                });
            }

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            toggle_dashboard,
            show_dashboard,
            hide_dashboard,
            minimize_to_tray,
            show_mascot,
            show_bubble,
            hide_bubble,
            position_bubble,
            quit_app,
            is_tauri,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
