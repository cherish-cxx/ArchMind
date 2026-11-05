package com.example.archmind.service.agent;

import java.util.Optional;

/**
 * 把逐行读到的 SSE 文本还原成事件块。
 *
 * <p>只认四个字段里的两个：{@code event} 与 {@code data}。{@code id} / {@code retry}
 * 不消费 —— Python 侧不发，发了也没有语义。</p>
 *
 * <p>三条容易写错的规则：</p>
 * <ul>
 *   <li>空行结块，且<b>连续空行不产生事件</b>；</li>
 *   <li>{@code data} 多行用 {@code \n} 拼接（JSON 会被拆成多行时靠它）；</li>
 *   <li>没有 {@code event} 字段时事件名默认 {@code message}（SSE 规范）。</li>
 * </ul>
 */
public final class SseBlockDecoder {

    private String name;
    private final StringBuilder data = new StringBuilder();

    /** 喂一行（不含行尾），遇到完整块时返回它。 */
    public Optional<SseEvent> accept(String rawLine) {
        String line = rawLine == null ? "" : rawLine;
        if (line.endsWith("\r")) {
            line = line.substring(0, line.length() - 1);
        }

        if (line.isEmpty()) {
            if (name == null && data.length() == 0) {
                return Optional.empty();
            }
            SseEvent event = new SseEvent(name == null ? "message" : name, data.toString());
            reset();
            return Optional.of(event);
        }
        if (line.startsWith(":")) {
            return Optional.empty();
        }

        int colon = line.indexOf(':');
        String field = colon < 0 ? line : line.substring(0, colon);
        String value = colon < 0 ? "" : line.substring(colon + 1);
        if (value.startsWith(" ")) {
            value = value.substring(1);
        }

        if ("event".equals(field)) {
            name = value;
        } else if ("data".equals(field)) {
            if (data.length() > 0) {
                data.append('\n');
            }
            data.append(value);
        }
        return Optional.empty();
    }

    private void reset() {
        name = null;
        data.setLength(0);
    }
}
