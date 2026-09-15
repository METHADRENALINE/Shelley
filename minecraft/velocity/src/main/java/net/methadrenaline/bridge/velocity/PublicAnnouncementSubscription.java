package net.methadrenaline.bridge.velocity;

import java.lang.reflect.InvocationTargetException;
import java.util.function.Consumer;
import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.serializer.plain.PlainTextComponentSerializer;

final class PublicAnnouncementSubscription {
    private PublicAnnouncementSubscription() {}

    static AutoCloseable subscribe(Object provider, String language, Consumer<String> publish)
            throws ReflectiveOperationException {
        Consumer<Component> listener = message -> {
            String text = PlainTextComponentSerializer.plainText().serialize(message);
            if (!text.isBlank()) {
                publish.accept(text);
            }
        };
        Object subscription = provider.getClass()
                .getMethod("subscribePublicAnnouncements", String.class, Consumer.class)
                .invoke(provider, language, listener);
        if (!(subscription instanceof AutoCloseable closeable)) {
            throw new InvocationTargetException(new IllegalStateException("Invalid public announcement subscription"));
        }
        return closeable;
    }
}
