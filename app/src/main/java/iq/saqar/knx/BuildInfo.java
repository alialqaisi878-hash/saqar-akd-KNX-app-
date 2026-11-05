package iq.saqar.knx;

import android.content.Context;

final class BuildInfo {
    static String name(Context c) {
        try { return c.getPackageManager().getPackageInfo(c.getPackageName(), 0).versionName; } catch (Exception e) { return ""; }
    }
}
