// GNOME Shell extension exposing a trusted D-Bus method for capturing
// full-screen screenshots, used by automated-ken-runner to drive
// screenshot-based smoke tests of snaps on this real desktop session.
//
// Why this exists: org.gnome.Shell.Screenshot (gnome-shell's own built-in
// D-Bus screenshot API) refuses calls from unsandboxed callers outside a
// short allowlist (gnome-screenshot, the xdg-desktop-portal backend) with
// "Screenshot is not allowed" — this applies even to a systemd --user
// service running in the same graphical session. Code running *inside*
// gnome-shell itself (i.e. an extension) has no such restriction, since
// it's the same trusted process. This extension just re-exposes that
// capability over its own D-Bus name so an external, unprivileged caller
// (the runner) can use it non-interactively.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Shell from 'gi://Shell';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const BUS_NAME = 'io.github.kenvandine.AutomatedKenScreenshot';
const OBJECT_PATH = '/io/github/kenvandine/AutomatedKenScreenshot';

const IFACE_XML = `
<node>
  <interface name="io.github.kenvandine.AutomatedKenScreenshot">
    <method name="Screenshot">
      <arg type="s" direction="in" name="filename"/>
      <arg type="b" direction="out" name="success"/>
    </method>
  </interface>
</node>`;

export default class AutomatedKenScreenshotExtension extends Extension {
    enable() {
        this._dbusImpl = Gio.DBusExportedObject.wrapJSObject(IFACE_XML, this);
        this._dbusImpl.export(Gio.DBus.session, OBJECT_PATH);
        this._ownerId = Gio.bus_own_name(
            Gio.BusType.SESSION,
            BUS_NAME,
            Gio.BusNameOwnerFlags.NONE,
            null,
            null,
            null,
        );
    }

    disable() {
        if (this._dbusImpl) {
            this._dbusImpl.unexport();
            this._dbusImpl = null;
        }
        if (this._ownerId) {
            Gio.bus_unown_name(this._ownerId);
            this._ownerId = null;
        }
    }

    // Async D-Bus method handler (GJS convention: <Name>Async(params, invocation)).
    ScreenshotAsync(params, invocation) {
        const [filename] = params;
        let stream;
        try {
            const file = Gio.File.new_for_path(filename);
            stream = file.replace(null, false, Gio.FileCreateFlags.REPLACE_DESTINATION, null);
        } catch (e) {
            logError(e, `AutomatedKenScreenshot: failed to open ${filename} for writing`);
            invocation.return_value(new GLib.Variant('(b)', [false]));
            return;
        }

        const screenshot = new Shell.Screenshot();
        screenshot.screenshot(false, stream, (obj, res) => {
            let success = false;
            try {
                [success] = screenshot.screenshot_finish(res);
            } catch (e) {
                logError(e, 'AutomatedKenScreenshot: screenshot_finish failed');
                success = false;
            }
            try {
                stream.close(null);
            } catch (e) {
                // best-effort close
            }
            invocation.return_value(new GLib.Variant('(b)', [success]));
        });
    }
}
