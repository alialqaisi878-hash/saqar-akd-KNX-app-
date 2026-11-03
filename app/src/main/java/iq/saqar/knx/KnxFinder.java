package iq.saqar.knx;

import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.NetworkInterface;
import java.net.SocketTimeoutException;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Finds KNX IP interfaces / routers on the LAN with a KNXnet/IP SEARCH_REQUEST. */
final class KnxFinder {
    static final class Found {
        final String ip, name;
        Found(String ip, String name) { this.ip = ip; this.name = name; }
    }

    static List<Found> search(int timeoutMs) {
        Map<String, Found> out = new LinkedHashMap<>();
        for (Inet4Address local : localAddresses()) {
            try (DatagramSocket s = new DatagramSocket(new InetSocketAddress(local, 0))) {
                s.setSoTimeout(400);
                byte[] ip = local.getAddress();
                int port = s.getLocalPort();
                byte[] req = {0x06, 0x10, 0x02, 0x01, 0x00, 0x0E,
                        0x08, 0x01, ip[0], ip[1], ip[2], ip[3], (byte) (port >> 8), (byte) port};
                InetAddress group = InetAddress.getByName("224.0.23.12");
                long end = System.currentTimeMillis() + timeoutMs;
                long nextSend = 0;
                byte[] buf = new byte[1024];
                while (System.currentTimeMillis() < end) {
                    if (System.currentTimeMillis() >= nextSend) {
                        s.send(new DatagramPacket(req, req.length, group, 3671));
                        nextSend = System.currentTimeMillis() + 1000;
                    }
                    DatagramPacket p = new DatagramPacket(buf, buf.length);
                    try {
                        s.receive(p);
                    } catch (SocketTimeoutException e) {
                        continue;
                    }
                    byte[] d = p.getData();
                    int n = p.getLength();
                    if (n < 14 || d[2] != 0x02 || (d[3] != 0x02 && d[3] != 0x0C)) continue;
                    String addr = (d[8] & 0xff) + "." + (d[9] & 0xff) + "." + (d[10] & 0xff) + "." + (d[11] & 0xff);
                    if (addr.equals("0.0.0.0")) addr = p.getAddress().getHostAddress();
                    String name = "";
                    if (n >= 68) name = new String(d, 38, 30, StandardCharsets.ISO_8859_1).replace("\0", "").trim();
                    out.put(addr, new Found(addr, name.isEmpty() ? "KNX IP" : name));
                }
            } catch (Exception ignored) { }
        }
        return new ArrayList<>(out.values());
    }

    private static List<Inet4Address> localAddresses() {
        List<Inet4Address> l = new ArrayList<>();
        try {
            for (NetworkInterface ni : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                if (!ni.isUp() || ni.isLoopback()) continue;
                for (InetAddress a : Collections.list(ni.getInetAddresses())) {
                    if (a instanceof Inet4Address && a.isSiteLocalAddress()) l.add((Inet4Address) a);
                }
            }
        } catch (Exception ignored) { }
        return l;
    }

    private KnxFinder() {}
}
