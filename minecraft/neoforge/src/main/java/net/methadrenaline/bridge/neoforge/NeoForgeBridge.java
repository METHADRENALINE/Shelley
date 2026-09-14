package net.methadrenaline.bridge.neoforge;

import net.minecraft.network.chat.Component;
import net.methadrenaline.bridge.BridgeClient;
import net.methadrenaline.bridge.BridgeConfig;
import net.neoforged.api.distmarker.Dist;
import net.neoforged.fml.common.Mod;
import net.neoforged.fml.loading.FMLPaths;
import net.neoforged.neoforge.common.NeoForge;
import net.neoforged.neoforge.event.server.ServerStartedEvent;
import net.neoforged.neoforge.event.server.ServerStoppingEvent;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

@Mod(value = "chat_bridge", dist = Dist.DEDICATED_SERVER)
public final class NeoForgeBridge {
    private static final Logger LOGGER = LoggerFactory.getLogger(NeoForgeBridge.class);
    private static BridgeClient bridge;

    public NeoForgeBridge() {
        NeoForge.EVENT_BUS.addListener(this::start);
        NeoForge.EVENT_BUS.addListener(this::stop);
    }

    private void start(ServerStartedEvent event) {
        try {
            BridgeConfig config = BridgeConfig.load(FMLPaths.CONFIGDIR.get().resolve("chat-bridge.json"));
            if (!config.enabled) {
                return;
            }
            var server = event.getServer();
            bridge = new BridgeClient(config, server::execute,
                    (username, text) -> server.getPlayerList().broadcastSystemMessage(Component.literal(BridgeClient.format(config.discordFormat, username, text)), false),
                    LOGGER::info);
            bridge.start();
        } catch (Exception exception) {
            LOGGER.error("Chat bridge could not start: {}", exception.getClass().getSimpleName());
        }
    }

    private void stop(ServerStoppingEvent event) {
        if (bridge != null) {
            bridge.close();
            bridge = null;
        }
    }

    public static void publish(String kind, Component message) {
        if (bridge != null) {
            bridge.publish(kind, message.getString());
        }
    }
}
