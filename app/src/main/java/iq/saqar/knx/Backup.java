package iq.saqar.knx;

import android.content.ContentResolver;
import android.content.ContentUris;
import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.net.Uri;
import android.os.Build;
import android.os.Environment;
import android.provider.MediaStore;
import android.util.Log;

import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;

/**
 * Keeps a copy of config.json (with the house key) and project.json (the map) in
 * Downloads/KNXProg/KNXProg-<house>.json, so a reinstall or a new tablet can restore the
 * same house from the settings screen instead of starting over.
 */
final class Backup {
    private static String last = "";

    static synchronized void run(Context c) {
        if (Build.VERSION.SDK_INT < 29) return;
        try {
            JSONObject cfg = Config.load(c);
            String house = cfg.optString("house_id");
            if (house.isEmpty()) return;
            JSONObject b = new JSONObject();
            b.put("knxprog_backup", 1);
            b.put("config", cfg);
            File pj = new File(Config.dataDir(c), "project.json");
            if (pj.exists()) b.put("project", new JSONObject(Config.readAll(new FileInputStream(pj))));
            String data = b.toString();
            if (data.equals(last)) return;
            ContentResolver cr = c.getContentResolver();
            Uri coll = MediaStore.Downloads.EXTERNAL_CONTENT_URI;
            String name = "KNXProg-" + house + ".json";
            Uri uri = null;
            try (Cursor cur = cr.query(coll, new String[]{MediaStore.MediaColumns._ID},
                    MediaStore.MediaColumns.DISPLAY_NAME + "=?", new String[]{name}, null)) {
                if (cur != null && cur.moveToFirst()) uri = ContentUris.withAppendedId(coll, cur.getLong(0));
            }
            if (uri == null) {
                ContentValues v = new ContentValues();
                v.put(MediaStore.MediaColumns.DISPLAY_NAME, name);
                v.put(MediaStore.MediaColumns.MIME_TYPE, "application/json");
                v.put(MediaStore.MediaColumns.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS + "/KNXProg");
                uri = cr.insert(coll, v);
            }
            if (uri == null) return;
            try (OutputStream o = cr.openOutputStream(uri, "wt")) {
                o.write(data.getBytes(StandardCharsets.UTF_8));
            }
            last = data;
        } catch (Throwable t) {
            Log.w("SaqarKNX", "backup failed", t);
        }
    }

    /** Restores a backup file's content. Returns the restored config, or null when it is not a backup. */
    static JSONObject restore(Context c, JSONObject b) throws Exception {
        if (!b.has("knxprog_backup")) return null;
        JSONObject cfg = b.getJSONObject("config");
        cfg.put("open_browser", false);
        Config.copy(new java.io.ByteArrayInputStream(cfg.toString(2).getBytes("UTF-8")), Config.file(c));
        if (b.has("project")) {
            Config.copy(new java.io.ByteArrayInputStream(b.getJSONObject("project").toString().getBytes("UTF-8")),
                    new File(Config.dataDir(c), "project.json"));
        }
        return cfg;
    }

    private Backup() {}
}
