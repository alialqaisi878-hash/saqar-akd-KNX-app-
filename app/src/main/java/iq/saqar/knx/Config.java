package iq.saqar.knx;

import android.content.Context;

import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;

/** config.json lives in the app's private storage (files/data), next to project.json. */
final class Config {
    static final String DEFAULT_RELAY = "https://srv2056159.hstgr.cloud/knx";
    static final String PUBLIC_BASE = "https://knx.saqar-akd.com.iq/h/";

    static File dataDir(Context c) {
        File d = new File(c.getFilesDir(), "data");
        if (!d.exists()) d.mkdirs();
        return d;
    }

    static File file(Context c) { return new File(dataDir(c), "config.json"); }

    static JSONObject load(Context c) {
        try {
            File f = file(c);
            if (!f.exists()) return new JSONObject();
            return new JSONObject(readAll(new FileInputStream(f)));
        } catch (Exception e) {
            return new JSONObject();
        }
    }

    static boolean isConfigured(Context c) {
        JSONObject o = load(c);
        return o.optString("house_id").length() > 0 && o.optString("gateway_ip").length() > 0;
    }

    static void save(Context c, String houseId, String ip, String pw, String relay) throws Exception {
        JSONObject o = load(c); // keeps house_key and anything else already there
        o.put("house_id", houseId);
        o.put("gateway_ip", ip);
        o.put("remote_password", pw);
        o.put("relay_url", relay);
        if (!o.has("connection")) o.put("connection", "auto");
        if (!o.has("keyring_file")) o.put("keyring_file", "");
        if (!o.has("keyring_password")) o.put("keyring_password", "");
        o.put("port", 8080);
        o.put("open_browser", false);
        try (OutputStream out = new FileOutputStream(file(c))) {
            out.write(o.toString(2).getBytes(StandardCharsets.UTF_8));
        }
    }

    static String readAll(InputStream in) throws Exception {
        try (InputStream i = in) {
            java.io.ByteArrayOutputStream b = new java.io.ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = i.read(buf)) > 0) b.write(buf, 0, n);
            return b.toString("UTF-8");
        }
    }

    static void copy(InputStream in, File dest) throws Exception {
        try (InputStream i = in; OutputStream o = new FileOutputStream(dest)) {
            byte[] buf = new byte[8192];
            int n;
            while ((n = i.read(buf)) > 0) o.write(buf, 0, n);
        }
    }

    private Config() {}
}
