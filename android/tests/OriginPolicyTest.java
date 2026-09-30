package com.agentarena.mobile;

public final class OriginPolicyTest {
    private static int checks = 0;
    private static void check(boolean value, String label) {
        checks++;
        if (!value) throw new AssertionError(label);
    }
    public static void main(String[] args) {
        OriginPolicy policy = OriginPolicy.fromInput("  HTTPS://Arena.Example.com:443/  ");
        check(policy.origin().equals("https://arena.example.com"), "canonical origin");
        check(policy.allows("https://arena.example.com/api/session?x=1"), "same origin route");
        check(policy.allows("https://ARENA.EXAMPLE.COM:443/#agents"), "same origin port and case");
        for (String value : new String[]{"http://arena.example.com/", "https://arena.example.com.evil.test/", "https://evil.test/", "https://arena.example.com:444/", "https://user:pass@arena.example.com/", "file:///data/data/private", "content://secret", "javascript:alert(1)", "intent://test", "https://arena.example.com./", "https://arena.example.com\\@evil.test/", "https://arena.example.com/\n"}) {
            check(!policy.allows(value), "blocked navigation");
        }
        for (String value : new String[]{"", "http://example.com", "https://example.com/login", "https://example.com/?token=secret", "https://example.com/#token", "https://user:secret@example.com", "https://example.com:0", "https://example.com:99999", "https://example.com.", "https://exa mple.com", "https://example.com\\evil", "//example.com", "https://%65xample.com"}) {
            boolean rejected = false;
            try { OriginPolicy.fromInput(value); } catch (IllegalArgumentException e) { rejected = true; }
            check(rejected, "invalid server origin");
        }
        OriginPolicy alternate = OriginPolicy.fromInput("https://arena.example.com:8443");
        check(alternate.allows("https://arena.example.com:8443/api/state"), "explicit alternate HTTPS port");
        check(!alternate.allows("https://arena.example.com/"), "alternate port isolation");
        OriginPolicy ipv6 = OriginPolicy.fromInput("https://[2001:db8::1]:8443");
        check(ipv6.allows("https://[2001:db8::1]:8443/"), "IPv6 origin");
        check(!OriginPolicy.isExternalHttps("javascript:alert(1)"), "external schemes restricted");
        check(OriginPolicy.isExternalHttps("https://news.example.com/article"), "external HTTPS article");
        System.out.println("OriginPolicy: " + checks + " security checks passed.");
    }
}
