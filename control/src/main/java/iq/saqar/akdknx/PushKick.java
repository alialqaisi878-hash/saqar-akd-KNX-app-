package iq.saqar.akdknx;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Watchdog: brings the house-alerts service back if the phone killed it (swiped away, battery saver). */
public class PushKick extends BroadcastReceiver {
    @Override public void onReceive(Context c, Intent intent) {
        Push.start(c);
        Push.watchdog(c, 15 * 60 * 1000L);
    }
}
