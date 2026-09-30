package com.agentarena.mobile;

import java.net.URI;
import java.net.URISyntaxException;
import java.util.Locale;

/** Small, independently testable HTTPS origin policy; no Android dependencies. */
public final class OriginPolicy {
    private final String origin;
    private final String host;
    private final int port;

    private OriginPolicy(String origin, String host, int port) {
        this.origin = origin;
        this.host = host;
        this.port = port;
    }

    private static URI parse(String value) throws URISyntaxException {
        if (value == null || value.length() == 0 || value.length() > 4096) {
            throw new URISyntaxException("", "Invalid address length");
        }
        for (int i = 0; i < value.length(); i++) {
            char c = value.charAt(i);
            if (c <= 0x20 || c == 0x7f || c == '\\') {
                throw new URISyntaxException("", "Address contains prohibited characters");
            }
        }
        URI uri = new URI(value);
        if (!"https".equalsIgnoreCase(uri.getScheme()) || uri.getRawUserInfo() != null
                || uri.getHost() == null || uri.getHost().length() == 0
                || uri.getHost().endsWith(".") || uri.getPort() == 0
                || uri.getPort() < -1 || uri.getPort() > 65535) {
            throw new URISyntaxException("", "Use a valid HTTPS server address");
        }
        return uri;
    }

    public static OriginPolicy fromInput(String value) throws IllegalArgumentException {
        try {
            if (value == null) throw new URISyntaxException("", "Missing address");
            URI uri = parse(value.trim());
            String path = uri.getRawPath();
            if ((path != null && !path.isEmpty() && !"/".equals(path))
                    || uri.getRawQuery() != null || uri.getRawFragment() != null) {
                throw new URISyntaxException("", "Enter only the server origin");
            }
            String host = uri.getHost().toLowerCase(Locale.ROOT);
            int port = uri.getPort() == -1 ? 443 : uri.getPort();
            String origin = new URI("https", null, host, port == 443 ? -1 : port, null, null, null).toASCIIString();
            return new OriginPolicy(origin, host, port);
        } catch (URISyntaxException e) {
            throw new IllegalArgumentException("Enter an HTTPS server origin only, such as https://arena.example.com. Do not include a path, token, or sign-in details.");
        }
    }

    public boolean allows(String address) {
        try {
            URI uri = parse(address);
            int candidatePort = uri.getPort() == -1 ? 443 : uri.getPort();
            return host.equalsIgnoreCase(uri.getHost()) && candidatePort == port;
        } catch (URISyntaxException e) {
            return false;
        }
    }

    public static boolean isExternalHttps(String address) {
        try { parse(address); return true; }
        catch (URISyntaxException e) { return false; }
    }

    public String origin() { return origin; }
    public String displayHost() { return host + (port == 443 ? "" : ":" + port); }
}
