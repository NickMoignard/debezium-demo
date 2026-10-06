package demo.integrate;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.HexFormat;

/**
 * Deterministic surrogate keys (D12): sha256(entity:region:source_id), hex.
 * The same source row always gets the same key, so a rebuild reproduces
 * identical keys and no lookup state is needed.
 */
public final class Keys {
    private Keys() {}

    /** Null-guarded: a null source id yields a null key, not a hash of "null". */
    public static String entityKey(String entity, String regionCode, Object sourceId) {
        if (sourceId == null) {
            return null;
        }
        return sha256Hex(entity + ":" + regionCode + ":" + sourceId);
    }

    static String sha256Hex(String input) {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            return HexFormat.of().formatHex(digest.digest(input.getBytes(StandardCharsets.UTF_8)));
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }
}
