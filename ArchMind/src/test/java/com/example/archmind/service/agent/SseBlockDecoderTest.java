package com.example.archmind.service.agent;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * SSE 行解码：只做格式，不碰网络。
 *
 * <p>这层最容易静默写错的地方是「data 多行拼接」和「空行结块」——
 * 错了不会抛异常，只会让前端收到半截 JSON。</p>
 */
class SseBlockDecoderTest {

    private final SseBlockDecoder decoder = new SseBlockDecoder();

    @Test
    void parses_named_block() {
        assertTrue(decoder.accept("event: step").isEmpty());
        assertTrue(decoder.accept("data: {\"n\":1}").isEmpty());
        assertEquals(new SseEvent("step", "{\"n\":1}"), decoder.accept("").orElseThrow());
    }

    @Test
    void joins_multiline_data_and_ignores_comments() {
        assertTrue(decoder.accept(": keep-alive").isEmpty());
        decoder.accept("data: a");
        decoder.accept("data: b");
        assertEquals("a\nb", decoder.accept("").orElseThrow().data());
    }

    @Test
    void tolerates_crlf_and_defaults_to_message() {
        decoder.accept("data: x\r");
        assertEquals("message", decoder.accept("\r").orElseThrow().name());
    }

    @Test
    void resets_after_a_block() {
        decoder.accept("event: step");
        decoder.accept("data: 1");
        decoder.accept("");
        decoder.accept("data: 2");
        assertEquals(new SseEvent("message", "2"), decoder.accept("").orElseThrow());
    }
}
