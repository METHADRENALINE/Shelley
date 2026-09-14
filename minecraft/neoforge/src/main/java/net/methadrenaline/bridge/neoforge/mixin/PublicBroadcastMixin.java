package net.methadrenaline.bridge.neoforge.mixin;

import net.minecraft.network.chat.ChatType;
import net.minecraft.network.chat.Component;
import net.minecraft.network.chat.PlayerChatMessage;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.server.players.PlayerList;
import net.methadrenaline.bridge.neoforge.NeoForgeBridge;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

@Mixin(PlayerList.class)
public abstract class PublicBroadcastMixin {
    @Inject(method = "broadcastSystemMessage(Lnet/minecraft/network/chat/Component;Z)V", at = @At("RETURN"))
    private void publicBroadcast(Component message, boolean overlay, CallbackInfo ci) {
        if (!overlay) {
            NeoForgeBridge.publish("broadcast", message);
        }
    }

    @Inject(method = "broadcastChatMessage(Lnet/minecraft/network/chat/PlayerChatMessage;Lnet/minecraft/server/level/ServerPlayer;Lnet/minecraft/network/chat/ChatType$Bound;)V", at = @At("RETURN"))
    private void publicChat(PlayerChatMessage message, ServerPlayer sender, ChatType.Bound type, CallbackInfo ci) {
        if (!message.isFullyFiltered()) {
            NeoForgeBridge.publish("chat", type.decorate(message.decoratedContent()));
        }
    }
}
