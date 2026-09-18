// axbridge — one atomic accessibility snapshot, one guarded execution, over a
// long-lived JSON-RPC pipe. This is the desktop counterpart of a DOM snapshot:
// it turns a macOS app into an indexed table of elements and the operations
// each one actually supports, and it executes a chosen operation by path.
//
// The process stays alive for the whole session (`axbridge serve`) so a step
// costs one accessibility traversal, not a process launch.
//
// Build: scripts/build.sh (swiftc, no dependencies).
//
// Accessibility helpers follow `cu` (Max's computer-use skill), MIT.

import AppKit
import ApplicationServices
import CryptoKit
import Foundation

// MARK: - Accessibility primitives

func attr(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    AXUIElementCopyAttributeValue(element, name as CFString, &value)
    return value
}

func string(_ element: AXUIElement, _ name: String) -> String {
    guard let value = attr(element, name) else { return "" }
    if let text = value as? String { return text }
    if let number = value as? NSNumber { return number.stringValue }
    if let url = value as? URL { return url.absoluteString }
    return ""
}

func bool(_ element: AXUIElement, _ name: String) -> Bool? {
    attr(element, name) as? Bool
}

func children(_ element: AXUIElement) -> [AXUIElement] {
    (attr(element, kAXChildrenAttribute) as? [AXUIElement]) ?? []
}

func actionNames(_ element: AXUIElement) -> [String] {
    var names: CFArray?
    AXUIElementCopyActionNames(element, &names)
    return (names as? [String]) ?? []
}

func frame(_ element: AXUIElement) -> CGRect {
    var point = CGPoint.zero, size = CGSize.zero
    if let value = attr(element, kAXPositionAttribute) { AXValueGetValue(value as! AXValue, .cgPoint, &point) }
    if let value = attr(element, kAXSizeAttribute) { AXValueGetValue(value as! AXValue, .cgSize, &size) }
    return CGRect(origin: point, size: size)
}

func settable(_ element: AXUIElement, _ name: String) -> Bool {
    var flag = DarwinBoolean(false)
    AXUIElementIsAttributeSettable(element, name as CFString, &flag)
    return flag.boolValue
}

/// A one-line human description, reused as the staleness guard for a path.
func describe(_ element: AXUIElement) -> String {
    let role = string(element, kAXRoleAttribute)
    let title = string(element, kAXTitleAttribute)
    let desc = string(element, kAXDescriptionAttribute)
    var value = string(element, kAXValueAttribute)
    if value.count > 60 { value = String(value.prefix(57)) + "..." }
    var parts = [role]
    let label = !title.isEmpty ? title : desc
    if !label.isEmpty { parts.append("\"\(label)\"") }
    if !value.isEmpty, label != value { parts.append("=\"\(value.replacingOccurrences(of: "\n", with: "⏎"))\"") }
    return parts.joined(separator: " ")
}

func screenLocked() -> Bool {
    ((CGSessionCopyCurrentDictionary() as? [String: Any])?["CGSSessionScreenIsLocked"] as? Bool) ?? false
}

/// Which app is active, asked live.
///
/// `NSWorkspace.frontmostApplication` is kept current by notifications, which a
/// command-line process without a run loop never delivers: in a long-lived
/// `serve` it answers with whatever was frontmost at launch. The system-wide
/// accessibility element has no such cache, but it answers with nothing until
/// this process has an accessibility connection, so the window list — which is
/// ordered front to back and caches nothing — is the fallback.
func frontmostPID() -> pid_t {
    if let focused = attr(AXUIElementCreateSystemWide(), kAXFocusedApplicationAttribute) {
        var pid: pid_t = 0
        AXUIElementGetPid(focused as! AXUIElement, &pid)
        if pid != 0 { return pid }
    }
    for window in onScreenWindows() where (window[kCGWindowLayer as String] as? Int) == 0 {
        if let pid = window[kCGWindowOwnerPID as String] as? pid_t { return pid }
    }
    return 0
}

func onScreenWindows() -> [[String: Any]] {
    (CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]])
        ?? []
}

func frontmostApp() -> NSRunningApplication? {
    let pid = frontmostPID()
    return pid == 0 ? nil : NSRunningApplication(processIdentifier: pid)
}

// MARK: - Apps

struct BridgeError: Error { let message: String }

func runningApp(_ query: String) throws -> NSRunningApplication {
    let lower = query.lowercased()
    let apps = NSWorkspace.shared.runningApplications.filter { $0.activationPolicy == .regular }
    if let pid = Int32(query), let app = apps.first(where: { $0.processIdentifier == pid }) { return app }
    if let app = apps.first(where: {
        $0.localizedName?.lowercased() == lower || $0.bundleIdentifier?.lowercased() == lower
    }) { return app }
    let matches = apps.filter {
        ($0.localizedName ?? "").lowercased().contains(lower) || ($0.bundleIdentifier ?? "").lowercased().contains(lower)
    }
    if matches.count == 1 { return matches[0] }
    if matches.isEmpty { throw BridgeError(message: "no running app matches \"\(query)\"") }
    throw BridgeError(
        message: "\"\(query)\" is ambiguous: "
            + matches.map { "\($0.localizedName ?? "?") (pid \($0.processIdentifier))" }.joined(separator: ", "))
}

func appElement(_ app: NSRunningApplication) -> AXUIElement {
    AXUIElementCreateApplication(app.processIdentifier)
}

/// Resolve "0.3.1" — child indices from the application element.
func resolve(_ app: NSRunningApplication, _ path: String) throws -> AXUIElement {
    var element = appElement(app)
    guard !path.isEmpty else { return element }
    for part in path.split(separator: ".") {
        guard let index = Int(part) else { throw BridgeError(message: "bad path component \"\(part)\"") }
        let kids = children(element)
        guard index < kids.count else {
            throw BridgeError(message: "path \(path) is stale: child \(index) of \(kids.count) does not exist")
        }
        element = kids[index]
    }
    return element
}

// MARK: - The element table

/// Roles that accept typed text. Treating every text-ish control as editable
/// misclassifies checkboxes and buttons, so the role list decides, not the
/// presence of a settable value.
let editableRoles: Set<String> = [
    kAXTextFieldRole, kAXTextAreaRole, kAXComboBoxRole, "AXSearchField",
]

let checkableRoles: Set<String> = [kAXCheckBoxRole, kAXRadioButtonRole, "AXSwitch", kAXMenuItemRole]

let containerRoles: Set<String> = [
    kAXGroupRole, kAXSplitGroupRole, kAXScrollAreaRole, kAXToolbarRole, kAXTabGroupRole,
    kAXListRole, kAXOutlineRole, kAXTableRole, kAXWindowRole, kAXApplicationRole, "AXLayoutArea",
]

struct Element {
    var index: String
    var path: String
    var role: String
    var subrole: String
    var identifier: String
    var label: String
    var value: String
    var enabled: Bool
    var focused: Bool
    var checked: Bool?
    var selected: Bool?
    var frame: CGRect
    var operations: [String]
    var options: [[String: Any]]
    var describe: String

    var json: [String: Any] {
        var out: [String: Any] = [
            "index": index,
            "path": path,
            "role": role,
            "label": label,
            "value": value,
            "enabled": enabled,
            "operations": operations,
            "frame": [Int(frame.minX), Int(frame.minY), Int(frame.width), Int(frame.height)],
        ]
        if !subrole.isEmpty { out["subrole"] = subrole }
        if !identifier.isEmpty { out["identifier"] = identifier }
        if focused { out["focused"] = true }
        if let checked { out["checked"] = checked }
        if let selected { out["selected"] = selected }
        if !options.isEmpty { out["options"] = options }
        return out
    }
}

/// Standard window controls carry a verbose AXHelp ("this button also has an
/// action to zoom…") that would crowd out their actual identity.
let subroleNames: [String: String] = [
    "AXCloseButton": "Close window", "AXMinimizeButton": "Minimize window",
    "AXZoomButton": "Zoom window", "AXFullScreenButton": "Full screen",
    "AXToolbarButton": "Toolbar", "AXSortButton": "Sort",
]

/// The best available name for an element: its own title, its description, the
/// label element pointing at it, or — for a control that carries none — the
/// nearest static text inside it. This covers common labels, not the full
/// accessible-name algorithm.
func label(of element: AXUIElement, role: String) -> String {
    let subrole = string(element, kAXSubroleAttribute)
    if let name = subroleNames[subrole] { return name }
    for name in [kAXTitleAttribute, kAXDescriptionAttribute] {
        let text = string(element, name).trimmingCharacters(in: .whitespacesAndNewlines)
        if !text.isEmpty { return text }
    }
    if let titleElement = attr(element, kAXTitleUIElementAttribute) {
        let text = string(titleElement as! AXUIElement, kAXValueAttribute)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        if !text.isEmpty { return text }
    }
    for name in [kAXPlaceholderValueAttribute, "AXHelp"] {
        var text = string(element, name).trimmingCharacters(in: .whitespacesAndNewlines)
        if !text.isEmpty {
            if text.count > 80 { text = String(text.prefix(77)) + "..." }
            return text
        }
    }
    if role != kAXStaticTextRole {
        for child in children(element).prefix(4) where string(child, kAXRoleAttribute) == kAXStaticTextRole {
            let text = string(child, kAXValueAttribute).trimmingCharacters(in: .whitespacesAndNewlines)
            if !text.isEmpty { return text }
        }
    }
    return ""
}

/// Options of a popup button, when its menu is exposed while closed.
func popupOptions(_ element: AXUIElement, index: String) -> [[String: Any]] {
    var pool = children(element)
    if pool.count == 1, string(pool[0], kAXRoleAttribute) == kAXMenuRole { pool = children(pool[0]) }
    var out: [[String: Any]] = []
    for (i, item) in pool.enumerated() where string(item, kAXRoleAttribute) == kAXMenuItemRole {
        let title = string(item, kAXTitleAttribute).trimmingCharacters(in: .whitespacesAndNewlines)
        guard !title.isEmpty else { continue }
        out.append(["index": "\(index):\(out.count + 1)", "label": title, "child": i])
        if out.count >= 60 { break }
    }
    return out
}

func operations(for element: AXUIElement, role: String, enabled: Bool) -> [String] {
    guard enabled else { return [] }
    var out: [String] = []
    let available = Set(actionNames(element))
    if available.contains(kAXPressAction) { out.append("PRESS") }
    if editableRoles.contains(role), settable(element, kAXValueAttribute) { out.append("TYPE_TEXT") }
    if role == kAXPopUpButtonRole || role == "AXMenuButton" { out.append("SELECT") }
    if available.contains(kAXIncrementAction) { out.append("INCREMENT") }
    if available.contains(kAXDecrementAction) { out.append("DECREMENT") }
    return out
}

struct Snapshot {
    var app: String
    var pid: pid_t
    var bundle: String
    var window: String
    var windowFrame: CGRect
    var elements: [Element]
    var menus: [[String: Any]]
    var text: String
    var fingerprint: String
    var truncated: Bool
    var active: Bool
}

final class Walker {
    var elements: [Element] = []
    var texts: [String] = []
    var bounds: CGRect
    let limit: Int
    var truncated = false

    init(bounds: CGRect, limit: Int) {
        self.bounds = bounds
        self.limit = limit
    }

    func walk(_ element: AXUIElement, path: String, depth: Int, maxDepth: Int) {
        guard depth <= maxDepth else { return }
        let role = string(element, kAXRoleAttribute)
        if role == kAXMenuBarRole { return }
        let rect = frame(element)
        // Offscreen and zero-size elements do not fill the model's context.
        let visible = rect.width > 0 && rect.height > 0 && (depth == 0 || rect.intersects(bounds))
        if visible {
            if role == kAXStaticTextRole {
                let text = string(element, kAXValueAttribute).trimmingCharacters(in: .whitespacesAndNewlines)
                if !text.isEmpty, texts.count < 400 { texts.append(text) }
            }
            let enabled = bool(element, kAXEnabledAttribute) ?? true
            let ops = operations(for: element, role: role, enabled: enabled)
            if !ops.isEmpty || (role == kAXTextAreaRole) {
                if elements.count >= limit {
                    truncated = true
                } else {
                    let index = String(elements.count + 1)
                    var options: [[String: Any]] = []
                    if ops.contains("SELECT") { options = popupOptions(element, index: index) }
                    var value = string(element, kAXValueAttribute)
                    if value.count > 200 { value = String(value.prefix(197)) + "..." }
                    var checked: Bool?
                    if checkableRoles.contains(role), let number = attr(element, kAXValueAttribute) as? NSNumber {
                        checked = number.intValue != 0
                    }
                    elements.append(
                        Element(
                            index: index,
                            path: path,
                            role: role,
                            subrole: string(element, kAXSubroleAttribute),
                            identifier: string(element, kAXIdentifierAttribute),
                            label: label(of: element, role: role),
                            value: checked == nil ? value : "",
                            enabled: enabled,
                            focused: bool(element, kAXFocusedAttribute) ?? false,
                            checked: checked,
                            selected: bool(element, kAXSelectedAttribute),
                            frame: rect,
                            operations: options.isEmpty ? ops : ops,
                            options: options,
                            describe: describe(element)
                        ))
                }
            }
        }
        // A collapsed or offscreen container still hides no children worth reading.
        guard visible || depth == 0 || containerRoles.contains(role) else { return }
        for (i, child) in children(element).enumerated() {
            walk(child, path: path.isEmpty ? "\(i)" : "\(path).\(i)", depth: depth + 1, maxDepth: maxDepth)
        }
    }
}

/// Menu-bar items are readable while the menus are closed, and they are a real
/// part of a desktop action space. Reading them costs a second traversal, so a
/// snapshot reuses the last read until an operation invalidates it.
///
/// An inactive app does not validate its menus: every item comes back disabled,
/// and titles that depend on document state go stale. The app must be active
/// for this list to mean anything.
var menuCache: [pid_t: (titles: [[String: Any]], read: Date)] = [:]

func menuItems(_ app: NSRunningApplication, limit: Int) -> [[String: Any]] {
    if let cached = menuCache[app.processIdentifier], Date().timeIntervalSince(cached.read) < 5 {
        return cached.titles
    }
    var out: [[String: Any]] = []
    guard let bar = children(appElement(app)).first(where: { string($0, kAXRoleAttribute) == kAXMenuBarRole })
    else { return out }
    // Submenus that list the system's services or the user's file history are
    // never the goal and would crowd out the app's own commands.
    let skipped: Set<String> = ["Services", "Open Recent", "Share", "Speech", "Substitutions", "Transformations"]
    for top in children(bar) {
        let menuTitle = string(top, kAXTitleAttribute)
        // The Apple menu carries system-wide entries that are never the goal.
        guard !menuTitle.isEmpty, menuTitle != "Apple" else { continue }
        var pool = children(top)
        if pool.count == 1, string(pool[0], kAXRoleAttribute) == kAXMenuRole { pool = children(pool[0]) }
        for item in pool {
            guard string(item, kAXRoleAttribute) == kAXMenuItemRole else { continue }
            let title = string(item, kAXTitleAttribute).trimmingCharacters(in: .whitespacesAndNewlines)
            guard !title.isEmpty, !skipped.contains(title), bool(item, kAXEnabledAttribute) ?? true else { continue }
            let sub = children(item)
            let submenu = sub.count == 1 && string(sub[0], kAXRoleAttribute) == kAXMenuRole ? children(sub[0]) : []
            if submenu.isEmpty {
                out.append(["index": "m\(out.count + 1)", "path": "\(menuTitle)>\(title)", "label": "\(menuTitle) > \(title)"])
            } else {
                for leaf in submenu {
                    guard string(leaf, kAXRoleAttribute) == kAXMenuItemRole else { continue }
                    let leafTitle = string(leaf, kAXTitleAttribute).trimmingCharacters(in: .whitespacesAndNewlines)
                    guard !leafTitle.isEmpty, bool(leaf, kAXEnabledAttribute) ?? true else { continue }
                    out.append([
                        "index": "m\(out.count + 1)",
                        "path": "\(menuTitle)>\(title)>\(leafTitle)",
                        "label": "\(menuTitle) > \(title) > \(leafTitle)",
                    ])
                    if out.count >= limit { break }
                }
            }
            if out.count >= limit { break }
        }
        if out.count >= limit { break }
    }
    menuCache[app.processIdentifier] = (out, Date())
    return out
}

func digest(_ text: String) -> String {
    SHA256.hash(data: Data(text.utf8)).prefix(8).map { String(format: "%02x", $0) }.joined()
}

func snapshot(app query: String, windowIndex: Int?, includeMenus: Bool, limit: Int, maxDepth: Int) throws -> Snapshot {
    if screenLocked() {
        throw BridgeError(message: "screen is locked — the accessibility tree is empty while it stays locked")
    }
    let app = try runningApp(query)
    let root = appElement(app)
    let windows = children(root).filter { string($0, kAXRoleAttribute) == kAXWindowRole }
    guard !windows.isEmpty else {
        throw BridgeError(message: "\(app.localizedName ?? query) has no accessible window (not drawn yet?)")
    }
    let allChildren = children(root)
    let window: AXUIElement
    let windowPath: String
    if let windowIndex {
        guard windowIndex < allChildren.count else {
            throw BridgeError(message: "app has \(allChildren.count) top-level children")
        }
        window = allChildren[windowIndex]
        windowPath = "\(windowIndex)"
    } else {
        // The focused window, falling back to the first one the app exposes.
        let focused = (attr(root, kAXFocusedWindowAttribute)).map { $0 as! AXUIElement }
        let chosen = focused ?? windows[0]
        guard let position = allChildren.firstIndex(where: { CFEqual($0, chosen) }) else {
            throw BridgeError(message: "the focused window is not among the app's children")
        }
        window = chosen
        windowPath = "\(position)"
    }
    let bounds = frame(window)
    let walker = Walker(bounds: bounds, limit: limit)
    walker.walk(window, path: windowPath, depth: 0, maxDepth: maxDepth)
    // An inactive app reports every menu item as disabled, so offering menu
    // commands then would offer operations that cannot run.
    let active = frontmostPID() == app.processIdentifier
    let menus = includeMenus && active ? menuItems(app, limit: 200) : []
    var text = walker.texts.joined(separator: "\n")
    if text.count > 6000 { text = String(text.prefix(6000)) + "…" }

    // Semantic fingerprint: what the model was shown, not how many mutations
    // the app made. Geometry is deliberately excluded — a window that merely
    // moved has not changed state.
    let material =
        (string(window, kAXTitleAttribute)) + "\u{1}"
        + walker.elements.map {
            "\($0.role)|\($0.label)|\($0.value)|\($0.enabled)|\($0.checked.map(String.init) ?? "")|\($0.selected.map(String.init) ?? "")"
        }.joined(separator: "\u{2}") + "\u{1}" + text
    return Snapshot(
        app: app.localizedName ?? query,
        pid: app.processIdentifier,
        bundle: app.bundleIdentifier ?? "",
        window: string(window, kAXTitleAttribute),
        windowFrame: bounds,
        elements: walker.elements,
        menus: menus,
        text: text,
        fingerprint: digest(material),
        truncated: walker.truncated,
        active: active
    )
}

func snapshotJSON(_ snap: Snapshot) -> [String: Any] {
    [
        "app": snap.app,
        "pid": Int(snap.pid),
        "bundle": snap.bundle,
        "window": snap.window,
        "window_frame": [
            Int(snap.windowFrame.minX), Int(snap.windowFrame.minY),
            Int(snap.windowFrame.width), Int(snap.windowFrame.height),
        ],
        "elements": snap.elements.map { $0.json },
        "menus": snap.menus,
        "active": snap.active,
        "text": snap.text,
        "fingerprint": snap.fingerprint,
        "truncated": snap.truncated,
    ]
}

// MARK: - Execution

func sleepMs(_ ms: Int) { usleep(useconds_t(ms * 1000)) }

func scroll(_ element: AXUIElement, pid: pid_t, amount: Int) throws {
    let rect = frame(element)
    guard rect.width > 0, rect.height > 0 else { throw BridgeError(message: "nothing scrollable is visible") }
    guard let event = CGEvent(
        scrollWheelEvent2Source: nil, units: .pixel, wheelCount: 1, wheel1: Int32(amount), wheel2: 0, wheel3: 0)
    else { throw BridgeError(message: "could not build a scroll event") }
    event.location = CGPoint(x: rect.midX, y: rect.midY)
    event.postToPid(pid)
}

/// Find the scroll area covering the largest part of the window.
func mainScrollArea(_ window: AXUIElement) -> AXUIElement? {
    var best: (AXUIElement, CGFloat)?
    func visit(_ element: AXUIElement, depth: Int) {
        guard depth < 12 else { return }
        if string(element, kAXRoleAttribute) == kAXScrollAreaRole {
            let rect = frame(element)
            let area = rect.width * rect.height
            if area > (best?.1 ?? 0) { best = (element, area) }
        }
        for child in children(element) { visit(child, depth: depth + 1) }
    }
    visit(window, depth: 0)
    return best?.0
}

let keyCodes: [String: CGKeyCode] = [
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12, "w": 13,
    "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25,
    "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "return": 36,
    "enter": 36, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46,
    ".": 47, "tab": 48, "space": 49, "`": 50, "delete": 51, "backspace": 51, "esc": 53, "escape": 53,
    "home": 115, "pageup": 116, "forwarddelete": 117, "end": 119, "pagedown": 121,
    "left": 123, "right": 124, "down": 125, "up": 126,
]

func pressKey(_ combo: String, pid: pid_t) throws {
    var flags = CGEventFlags()
    var name = ""
    for part in combo.lowercased().split(separator: "+") {
        switch part {
        case "cmd", "command": flags.insert(.maskCommand)
        case "shift": flags.insert(.maskShift)
        case "alt", "opt", "option": flags.insert(.maskAlternate)
        case "ctrl", "control": flags.insert(.maskControl)
        default: name = String(part)
        }
    }
    guard let code = keyCodes[name] else { throw BridgeError(message: "unknown key \"\(name)\"") }
    guard let down = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: true),
        let up = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: false)
    else { throw BridgeError(message: "could not build a key event") }
    down.flags = flags
    up.flags = flags
    down.postToPid(pid)
    sleepMs(20)
    up.postToPid(pid)
}

func typeText(_ text: String, pid: pid_t) {
    for scalar in text.unicodeScalars {
        var chars = Array(String(scalar).utf16)
        guard let down = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: true),
            let up = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: false)
        else { continue }
        down.keyboardSetUnicodeString(stringLength: chars.count, unicodeString: &chars)
        up.keyboardSetUnicodeString(stringLength: chars.count, unicodeString: &chars)
        down.postToPid(pid)
        sleepMs(4)
        up.postToPid(pid)
        sleepMs(4)
    }
}

// MARK: - Pixels, for apps that publish nothing

/// The on-screen window of an app, in global points, with its CGWindow id.
func windowRect(_ app: NSRunningApplication) throws -> (id: CGWindowID, rect: CGRect) {
    for window in onScreenWindows() where (window[kCGWindowLayer as String] as? Int) == 0 {
        guard (window[kCGWindowOwnerPID as String] as? pid_t) == app.processIdentifier else { continue }
        let bounds = window[kCGWindowBounds as String] as? [String: CGFloat] ?? [:]
        let rect = CGRect(
            x: bounds["X"] ?? 0, y: bounds["Y"] ?? 0,
            width: bounds["Width"] ?? 0, height: bounds["Height"] ?? 0)
        guard rect.width > 1, rect.height > 1 else { continue }
        return (CGWindowID(window[kCGWindowNumber as String] as? Int ?? 0), rect)
    }
    throw BridgeError(message: "\(app.localizedName ?? "the app") has no on-screen window to capture")
}

/// Capture one window as a JPEG, downscaled to `width` points.
///
/// The model is shown a picture whose coordinate space is the window's own, so
/// a point it names converts back to the screen by one offset and one scale.
func capture(_ app: NSRunningApplication, width: Int, quality: Double) throws -> [String: Any] {
    guard CGPreflightScreenCaptureAccess() else {
        throw BridgeError(
            message: "Screen Recording permission is missing, so a capture would be blank. "
                + "System Settings > Privacy & Security > Screen Recording: enable the app running this "
                + "shell, then restart it.")
    }
    let (id, rect) = try windowRect(app)
    let file = FileManager.default.temporaryDirectory
        .appendingPathComponent("jev-cu-\(UUID().uuidString).png")
    defer { try? FileManager.default.removeItem(at: file) }
    let task = Process()
    task.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
    task.arguments = ["-x", "-o", "-l", "\(id)", file.path]
    try? task.run()
    task.waitUntilExit()
    guard task.terminationStatus == 0, let raw = NSImage(contentsOf: file) else {
        throw BridgeError(message: "screencapture failed (\(task.terminationStatus))")
    }
    guard let source = raw.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        throw BridgeError(message: "could not read the captured image")
    }
    // The capture is in pixels; the window is in points. Downscale to a width
    // the model can read cheaply and record the factor back to screen points.
    let target = min(width, source.width)
    let height = Int((Double(source.height) / Double(source.width) * Double(target)).rounded())
    guard
        let context = CGContext(
            data: nil, width: target, height: height, bitsPerComponent: 8, bytesPerRow: 0,
            space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)
    else { throw BridgeError(message: "could not build a scaling context") }
    context.interpolationQuality = .high
    context.draw(source, in: CGRect(x: 0, y: 0, width: target, height: height))
    guard let scaled = context.makeImage() else { throw BridgeError(message: "could not scale the capture") }
    let bitmap = NSBitmapImageRep(cgImage: scaled)
    guard
        let jpeg = bitmap.representation(
            using: .jpeg, properties: [.compressionFactor: quality])
    else { throw BridgeError(message: "could not encode the capture") }
    return [
        "thumbnail": thumbnail(scaled),
        "image": jpeg.base64EncodedString(),
        "media_type": "image/jpeg",
        "image_width": target,
        "image_height": height,
        // Multiply an image point by this and add the origin to reach the screen.
        "scale": Double(rect.width) / Double(target),
        "origin": [Double(rect.minX), Double(rect.minY)],
        "window_id": Int(id),
        "window_size": [Double(rect.width), Double(rect.height)],
        "window_frame": [Int(rect.minX), Int(rect.minY), Int(rect.width), Int(rect.height)],
        "bytes": jpeg.count,
    ]
}

/// The frontmost ordinary window covering a screen point, if any.
///
/// `CGWindowListCopyWindowInfo` returns windows front to back, so the first
/// match is what the window server would hand a click at that point.
func frontWindow(at point: CGPoint) -> (id: CGWindowID, pid: pid_t)? {
    for window in onScreenWindows() where (window[kCGWindowLayer as String] as? Int) == 0 {
        let bounds = window[kCGWindowBounds as String] as? [String: CGFloat] ?? [:]
        let rect = CGRect(
            x: bounds["X"] ?? 0, y: bounds["Y"] ?? 0,
            width: bounds["Width"] ?? 0, height: bounds["Height"] ?? 0)
        guard rect.contains(point) else { continue }
        return (
            CGWindowID(window[kCGWindowNumber as String] as? Int ?? 0),
            window[kCGWindowOwnerPID as String] as? pid_t ?? 0
        )
    }
    return nil
}

/// A 16x16 greyscale reduction of the capture.
///
/// An Electron window's accessibility fingerprint barely moves no matter what
/// happens inside it, so the tree cannot tell whether a click landed. Comparing
/// two of these can: it is coarse enough to ignore a caret blink and fine
/// enough to notice a panel opening.
func thumbnail(_ image: CGImage) -> [Int] {
    let side = 16
    var pixels = [UInt8](repeating: 0, count: side * side)
    guard
        let context = CGContext(
            data: &pixels, width: side, height: side, bitsPerComponent: 8, bytesPerRow: side,
            space: CGColorSpaceCreateDeviceGray(), bitmapInfo: CGImageAlphaInfo.none.rawValue)
    else { return [] }
    context.interpolationQuality = .medium
    context.draw(image, in: CGRect(x: 0, y: 0, width: side, height: side))
    return pixels.map { Int($0) }
}

/// Click a point named in the captured image's coordinate space.
///
/// This moves the real pointer, and there is no way around it. A mouse event
/// posted to a process is ignored by ordinary controls — Calculator's keypad
/// does not react to one whether the app is frontmost or not — so a click that
/// actually lands has to go through the window server, which has exactly one
/// cursor and hands the event to whatever is under it.
///
/// Hence the two guards below: the app must be frontmost, and the point must
/// not be covered. Everything on the accessibility path stays quiet and
/// background; this does not, and cannot.
func clickImagePoint(
    _ app: NSRunningApplication, x: Double, y: Double, scale: Double,
    capturedWindow: CGWindowID?, capturedSize: [Double]?
) throws -> [String: Any] {
    guard frontmostPID() == app.processIdentifier else {
        throw BridgeError(
            message: "refused: a real click goes wherever the pointer is, and "
                + "\(app.localizedName ?? "the app") is not frontmost. Activate it first.")
    }
    let (id, rect) = try windowRect(app)
    // The point was named in a picture of one window at one size. If either
    // has changed, the scale that came with the picture no longer maps that
    // point anywhere meaningful — the click would land by coincidence.
    if let capturedWindow, capturedWindow != id {
        throw BridgeError(message: "refused: the window changed since the screenshot; capture again")
    }
    if let capturedSize, capturedSize.count == 2 {
        let moved = abs(capturedSize[0] - Double(rect.width)) > 1 || abs(capturedSize[1] - Double(rect.height)) > 1
        if moved {
            throw BridgeError(
                message: "refused: the window was \(Int(capturedSize[0]))x\(Int(capturedSize[1])) when the "
                    + "screenshot was taken and is now \(Int(rect.width))x\(Int(rect.height)); capture again")
        }
    }
    let point = CGPoint(x: rect.minX + x * scale, y: rect.minY + y * scale)
    guard rect.insetBy(dx: -2, dy: -2).contains(point) else {
        throw BridgeError(
            message: "point \(Int(point.x)),\(Int(point.y)) is outside the window "
                + "\(Int(rect.minX)),\(Int(rect.minY)) \(Int(rect.width))x\(Int(rect.height))")
    }
    // A point inside the window is not a point on the window. The window list
    // is ordered front to back, so whatever owns this point first is what a
    // click would actually hit — and a click that lands in somebody else's
    // window is the worst thing this program can do.
    if let covering = frontWindow(at: point), covering.id != id {
        let owner = NSRunningApplication(processIdentifier: covering.pid)?.localizedName ?? "another app"
        throw BridgeError(
            message: "refused: \(Int(point.x)),\(Int(point.y)) is covered by \(owner). "
                + "Raise \(app.localizedName ?? "the app") first, or choose a point that is not occluded.")
    }
    let restore = CGEvent(source: nil)?.location
    for type in [CGEventType.mouseMoved, .leftMouseDown, .leftMouseUp] {
        guard let event = CGEvent(mouseEventSource: nil, mouseType: type, mouseCursorPosition: point, mouseButton: .left)
        else { throw BridgeError(message: "could not build a click event") }
        event.setIntegerValueField(.mouseEventClickState, value: 1)
        event.post(tap: .cghidEventTap)
        sleepMs(type == .mouseMoved ? 30 : 40)
    }
    // Put the pointer back where the user left it.
    if let restore, let move = CGEvent(
        mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: restore, mouseButton: .left) {
        sleepMs(40)
        move.post(tap: .cghidEventTap)
    }
    return [
        "ok": true,
        "detail": "clicked \(Int(point.x)),\(Int(point.y)) in \(app.localizedName ?? "the app")",
        "mechanism": "HID (real pointer)",
    ]
}

func pressMenuPath(_ app: NSRunningApplication, _ path: String) throws -> String {
    let titles = path.split(separator: ">").map { $0.trimmingCharacters(in: .whitespaces) }
    guard let bar = children(appElement(app)).first(where: { string($0, kAXRoleAttribute) == kAXMenuBarRole })
    else { throw BridgeError(message: "no menu bar exposed") }
    var element = bar
    for title in titles {
        var pool = children(element)
        if pool.count == 1, string(pool[0], kAXRoleAttribute) == kAXMenuRole { pool = children(pool[0]) }
        guard
            let next = pool.first(where: { string($0, kAXTitleAttribute).lowercased() == title.lowercased() })
                ?? pool.first(where: { string($0, kAXTitleAttribute).lowercased().hasPrefix(title.lowercased()) })
        else { throw BridgeError(message: "no menu item \"\(title)\" under \(describe(element))") }
        element = next
    }
    guard bool(element, kAXEnabledAttribute) ?? true else {
        throw BridgeError(message: "menu item \(path) is disabled")
    }
    let result = AXUIElementPerformAction(element, kAXPressAction as CFString)
    guard result == .success else { throw BridgeError(message: "menu press failed: AXError \(result.rawValue)") }
    return describe(element)
}

/// Execute one operation. The guard is scoped on purpose: the element at the
/// path must still be the element the decision was made about. Unrelated parts
/// of the window are allowed to have changed.
func execute(_ request: [String: Any]) throws -> [String: Any] {
    guard let appName = request["app"] as? String, let op = request["op"] as? String else {
        throw BridgeError(message: "act needs app and op")
    }
    let app = try runningApp(appName)
    let pid = app.processIdentifier
    if screenLocked() { throw BridgeError(message: "screen is locked — no operation can reach the app") }
    // Menu titles and enabled states are revalidated by the app itself; any
    // operation can change them ("Make Rich Text" becomes "Make Plain Text").
    menuCache[pid] = nil

    if op == "MENU" {
        guard let path = request["menu"] as? String else { throw BridgeError(message: "MENU needs a menu path") }
        return ["ok": true, "detail": try pressMenuPath(app, path), "mechanism": "AXPress(menu)"]
    }
    if op == "KEY" {
        guard let combo = request["key"] as? String else { throw BridgeError(message: "KEY needs a combo") }
        try pressKey(combo, pid: pid)
        return ["ok": true, "detail": "key \(combo)", "mechanism": "CGEvent→pid"]
    }
    if op == "CLICK_POINT" {
        guard let x = request["x"] as? Double, let y = request["y"] as? Double,
            let scale = request["scale"] as? Double
        else { throw BridgeError(message: "CLICK_POINT needs x, y and the capture's scale") }
        return try clickImagePoint(
            app, x: x, y: y, scale: scale,
            capturedWindow: (request["window_id"] as? Int).map { CGWindowID($0) },
            capturedSize: request["window_size"] as? [Double])
    }
    if op == "TYPE_KEYS" {
        // No element to set a value on; the keystrokes go wherever the app's
        // own focus is, which is why this needs a visible confirmation after.
        guard let text = request["text"] as? String else { throw BridgeError(message: "TYPE_KEYS needs text") }
        typeText(text, pid: pid)
        return ["ok": true, "detail": "typed \(text.count) characters", "mechanism": "CGEvent→pid (blind)"]
    }
    if op == "SCROLL_UP" || op == "SCROLL_DOWN" {
        let root = appElement(app)
        let focused = (attr(root, kAXFocusedWindowAttribute)).map { $0 as! AXUIElement }
            ?? children(root).first(where: { string($0, kAXRoleAttribute) == kAXWindowRole })
        guard let window = focused, let area = mainScrollArea(window) ?? focused else {
            throw BridgeError(message: "no scrollable area found")
        }
        try scroll(area, pid: pid, amount: op == "SCROLL_UP" ? 240 : -240)
        return ["ok": true, "detail": op.lowercased(), "mechanism": "CGEvent→pid"]
    }

    guard let path = request["path"] as? String else { throw BridgeError(message: "\(op) needs an element path") }
    let element = try resolve(app, path)
    let current = describe(element)
    // A label alone does not identify a control: a list that reordered can put
    // a different row with the same name under the same path. Role and
    // AXIdentifier are what actually pin it down, when the app supplies them.
    if let wantRole = request["expect_role"] as? String, !wantRole.isEmpty {
        let actual = string(element, kAXRoleAttribute)
        if actual != wantRole {
            throw BridgeError(message: "stale: \(path) was a \(wantRole) and is now a \(actual)")
        }
    }
    if let wantID = request["expect_id"] as? String, !wantID.isEmpty {
        let actual = string(element, kAXIdentifierAttribute)
        if actual != wantID {
            throw BridgeError(
                message: "stale: \(path) had identifier \"\(wantID)\" and now has "
                    + (actual.isEmpty ? "none" : "\"\(actual)\""))
        }
    }
    if let expect = request["expect"] as? String, !expect.isEmpty {
        // Trees shift between observing and acting; a stale path presses the
        // wrong thing. Refuse rather than guess.
        let hay = current.lowercased()
        let needle = expect.lowercased()
        if !hay.contains(needle) {
            throw BridgeError(message: "stale: \(path) is now \(current), which no longer mentions \"\(expect)\"")
        }
    }
    guard bool(element, kAXEnabledAttribute) ?? true else {
        throw BridgeError(message: "\(current) is disabled")
    }

    switch op {
    case "PRESS":
        let available = actionNames(element)
        guard available.contains(kAXPressAction) else {
            throw BridgeError(message: "\(current) offers no AXPress; it has \(available)")
        }
        let result = AXUIElementPerformAction(element, kAXPressAction as CFString)
        guard result == .success else { throw BridgeError(message: "press failed: AXError \(result.rawValue)") }
        return ["ok": true, "detail": current, "mechanism": "AXPress"]
    case "INCREMENT", "DECREMENT":
        let action = op == "INCREMENT" ? kAXIncrementAction : kAXDecrementAction
        let result = AXUIElementPerformAction(element, action as CFString)
        guard result == .success else { throw BridgeError(message: "\(op) failed: AXError \(result.rawValue)") }
        return ["ok": true, "detail": current, "mechanism": action]
    case "TYPE_TEXT":
        guard let text = request["text"] as? String else { throw BridgeError(message: "TYPE_TEXT needs text") }
        // This operation means "replace the whole value". Writing the value is
        // the only mechanism that means exactly that; the keyboard fallback has
        // to select the old contents first, or it would quietly become an
        // insert and leave the field holding something nobody asked for.
        let focused = AXUIElementSetAttributeValue(element, kAXFocusedAttribute as CFString, kCFBooleanTrue)
        let result = AXUIElementSetAttributeValue(element, kAXValueAttribute as CFString, text as CFTypeRef)
        var mechanism = "AXValue"
        if result != .success {
            guard focused == .success else {
                throw BridgeError(
                    message: "the field refused both the value (AXError \(result.rawValue)) and focus "
                        + "(AXError \(focused.rawValue)); typing now would go somewhere unknown")
            }
            try pressKey("cmd+a", pid: pid)
            sleepMs(30)
            typeText(text, pid: pid)
            sleepMs(60)
            mechanism = "CGEvent→pid (select all, then type)"
        }
        // Read back and require the value to actually be the value. A field
        // that truncated, decorated or ignored the write is not a success, and
        // a substring test would call two of those three a success.
        let after = string(element, kAXValueAttribute)
        let settled = after.trimmingCharacters(in: .whitespacesAndNewlines)
        let wanted = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if settled == wanted {
            return ["ok": true, "detail": describe(element), "mechanism": mechanism, "verified": true]
        }
        // Some fields expose no readable value at all. That is not a failure,
        // but it is not confirmation either, and the caller must be told which.
        if after.isEmpty, !settable(element, kAXValueAttribute) {
            return [
                "ok": true, "detail": describe(element), "mechanism": mechanism, "verified": false,
                "unverified": "this field exposes no readable value, so the write could not be confirmed",
            ]
        }
        throw BridgeError(
            message: "the field did not take the value: wanted \"\(wanted)\", holds \"\(settled)\"")
    case "SELECT":
        guard let choice = request["option"] as? Int else { throw BridgeError(message: "SELECT needs an option") }
        var pool = children(element)
        if pool.count == 1, string(pool[0], kAXRoleAttribute) == kAXMenuRole { pool = children(pool[0]) }
        guard choice < pool.count else { throw BridgeError(message: "option \(choice) is gone; re-observe") }
        let item = pool[choice]
        let result = AXUIElementPerformAction(item, kAXPressAction as CFString)
        guard result == .success else { throw BridgeError(message: "select failed: AXError \(result.rawValue)") }
        return ["ok": true, "detail": describe(item), "mechanism": "AXPress(option)"]
    default:
        throw BridgeError(message: "unknown operation \(op)")
    }
}

// MARK: - RPC

func statusJSON() -> [String: Any] {
    let front = frontmostApp()
    return [
        "accessibility": AXIsProcessTrusted(),
        "screen_recording": CGPreflightScreenCaptureAccess(),
        "locked": screenLocked(),
        "frontmost": front?.localizedName ?? "",
        "frontmost_pid": Int(front?.processIdentifier ?? 0),
    ]
}

func handle(_ request: [String: Any]) -> [String: Any] {
    let id = request["id"] as? Int ?? 0
    let method = request["method"] as? String ?? ""
    do {
        switch method {
        case "status":
            return ["id": id, "ok": true, "result": statusJSON()]
        case "apps":
            let front = frontmostPID()
            let apps = NSWorkspace.shared.runningApplications.filter { $0.activationPolicy == .regular }
                .map {
                    [
                        "pid": Int($0.processIdentifier), "name": $0.localizedName ?? "",
                        "bundle": $0.bundleIdentifier ?? "", "frontmost": $0.processIdentifier == front,
                    ] as [String: Any]
                }
            return ["id": id, "ok": true, "result": ["apps": apps]]
        case "activate":
            guard let name = request["app"] as? String else { throw BridgeError(message: "activate needs app") }
            let app = try runningApp(name)
            app.activate()
            sleepMs(250)
            return [
                "id": id, "ok": true,
                "result": ["frontmost": frontmostPID() == app.processIdentifier],
            ]
        case "snapshot":
            guard let name = request["app"] as? String else { throw BridgeError(message: "snapshot needs app") }
            let snap = try snapshot(
                app: name,
                windowIndex: request["window"] as? Int,
                includeMenus: (request["menus"] as? Bool) ?? true,
                limit: (request["limit"] as? Int) ?? 250,
                maxDepth: (request["depth"] as? Int) ?? 18
            )
            return ["id": id, "ok": true, "result": snapshotJSON(snap)]
        case "fingerprint":
            // The same semantic material as a snapshot, without building the
            // element table or re-reading the menus: the cheap staleness check.
            guard let name = request["app"] as? String else { throw BridgeError(message: "fingerprint needs app") }
            let snap = try snapshot(
                app: name,
                windowIndex: request["window"] as? Int,
                includeMenus: false,
                limit: (request["limit"] as? Int) ?? 250,
                maxDepth: (request["depth"] as? Int) ?? 18
            )
            return ["id": id, "ok": true, "result": ["fingerprint": snap.fingerprint, "window": snap.window]]
        case "capture":
            guard let name = request["app"] as? String else { throw BridgeError(message: "capture needs app") }
            let app = try runningApp(name)
            return [
                "id": id, "ok": true,
                "result": try capture(
                    app,
                    width: (request["width"] as? Int) ?? 1000,
                    quality: (request["quality"] as? Double) ?? 0.6),
            ]
        case "act":
            return ["id": id, "ok": true, "result": try execute(request)]
        case "ping":
            return ["id": id, "ok": true, "result": ["pong": true]]
        default:
            throw BridgeError(message: "unknown method \"\(method)\"")
        }
    } catch let error as BridgeError {
        return ["id": id, "ok": false, "error": error.message]
    } catch {
        return ["id": id, "ok": false, "error": "\(error)"]
    }
}

func emit(_ payload: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys]) else { return }
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
}

let arguments = Array(CommandLine.arguments.dropFirst())
let usage = """
axbridge — accessibility snapshots and guarded execution for jev-computer-use

  axbridge serve                 JSON-RPC over stdin/stdout, one request per line
  axbridge status                permissions and frontmost app, as JSON
  axbridge snapshot <app>        one indexed element table, as JSON

Methods in serve mode: status, apps, activate, snapshot, act, ping.
"""

switch arguments.first {
case "serve":
    // A long-lived process: a step costs one traversal, not a process launch.
    setvbuf(stdout, nil, _IOLBF, 0)
    while let line = readLine(strippingNewline: true) {
        guard !line.trimmingCharacters(in: .whitespaces).isEmpty else { continue }
        guard let data = line.data(using: .utf8),
            let request = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        else {
            emit(["id": 0, "ok": false, "error": "malformed request"])
            continue
        }
        emit(handle(request))
    }
case "status":
    emit(statusJSON())
case "snapshot":
    guard arguments.count > 1 else { print(usage); exit(1) }
    emit(handle(["id": 1, "method": "snapshot", "app": arguments[1]]))
default:
    print(usage)
    exit(arguments.isEmpty ? 0 : 1)
}
