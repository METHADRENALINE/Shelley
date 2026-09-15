package net.methadrenaline.bridge;

import com.google.gson.Gson;
import java.io.IOException;
import java.net.URI;
import java.nio.file.Files;
import java.nio.file.Path;

public final class BridgeConfig {
    public boolean enabled = false;
    public String endpoint = "";
    public String tokenFile = "";
    public String certificateFile = "";
    public int pollMillis = 1000;
    public int requestTimeoutSeconds = 10;
    public int messageTtlSeconds = 120;
    public int maxQueue = 500;
    public int maxMessageLength = 1500;
    public boolean playerChat = true;
    public boolean publicBroadcasts = true;
    public String publicAnnouncementLanguage = "en_us";
    public String discordFormat = "[Discord] <{username}> {text}";
    public String globalChannel = "ma:core";
    public String globalEvent = "global-chat";

    public static BridgeConfig load(Path path) throws IOException {
        if (!Files.isRegularFile(path)) {
            return new BridgeConfig();
        }
        BridgeConfig config = new Gson().fromJson(Files.readString(path), BridgeConfig.class);
        if (config == null) {
            throw new IOException("Empty bridge configuration");
        }
        config.validate();
        return config;
    }

    public void validate() {
        if (!enabled) {
            return;
        }
        URI uri = URI.create(endpoint);
        if (!"https".equals(uri.getScheme()) || uri.getHost() == null || uri.getUserInfo() != null || uri.getQuery() != null) {
            throw new IllegalArgumentException("Bridge endpoint must use HTTPS");
        }
        if (pollMillis < 200 || requestTimeoutSeconds < 1 || requestTimeoutSeconds > 60
                || messageTtlSeconds < 10 || messageTtlSeconds > 3600 || maxQueue < 1 || maxQueue > 5000
                || maxMessageLength < 1 || maxMessageLength > 1800) {
            throw new IllegalArgumentException("Invalid bridge limits");
        }
        if (!Files.isRegularFile(Path.of(tokenFile)) || !Files.isRegularFile(Path.of(certificateFile))) {
            throw new IllegalArgumentException("Bridge token and certificate files are required");
        }
        if (!discordFormat.contains("{username}") || !discordFormat.contains("{text}")) {
            throw new IllegalArgumentException("Discord format needs {username} and {text}");
        }
        if (publicAnnouncementLanguage == null || !publicAnnouncementLanguage.matches("[a-z]{2}_[a-z]{2}")) {
            throw new IllegalArgumentException("Public announcement language must use a locale such as en_us");
        }
    }
}
