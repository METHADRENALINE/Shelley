package net.methadrenaline.bridge;

import static org.junit.jupiter.api.Assertions.*;
import org.junit.jupiter.api.Test;

class BridgeClientTest {
    @Test
    void usernamesStaySeparateFromMinecraftAccounts() {
        assertEquals("[Discord] <discord_name> hello", BridgeClient.format("[Discord] <{username}> {text}", "discord_name", "hello"));
    }

    @Test
    void controlCharactersCannotSpoofExtraNames() {
        assertEquals("<one two> text", BridgeClient.format("<{username}> {text}", "one\ntwo", "text"));
        assertEquals("safe", BridgeClient.clean("§csafe\u202e\u0000", 100));
    }

    @Test
    void lengthLimitKeepsUnicodeCharactersIntact() {
        assertEquals("a\ud83d\ude00", BridgeClient.clean("a\ud83d\ude00b", 2));
    }

    @Test
    void insecureTransportIsRejected() {
        BridgeConfig config = new BridgeConfig();
        config.enabled = true;
        config.endpoint = "http://localhost/bridge";
        assertThrows(IllegalArgumentException.class, config::validate);
    }
}
