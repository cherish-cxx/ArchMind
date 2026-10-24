package com.example.archmind.dto.response;

import java.util.List;

import org.junit.jupiter.api.Test;
import tools.jackson.databind.ObjectMapper;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * R2 的防漏测试：Python 发来的 {@code sections} 必须真的被 Java DTO 接住、再原样发出去。
 *
 * <p>和 {@link AgentResponseGraphTest} 同一条理由：Spring Boot 默认忽略未知字段，
 * DTO 少写一个字段不会报错、只会静默丢。反序列化 + 序列化两个方向都要测 ——
 * 少了 getter，读得进来也发不出去。</p>
 */
class AgentResponseSectionsTest {

    private final ObjectMapper mapper = new ObjectMapper();

    @Test
    void sectionsSurviveBothDirections() throws Exception {
        String json = """
                {
                  "conversationId": "c1",
                  "message": "## 结论\\n\\n### 路径\\n\\n`A` 调用 `B`。",
                  "headline": "A 只调用了 B",
                  "sections": [
                    { "title": "路径", "body": "`A` 调用 `B`。", "citedUids": ["com.demo.A", "com.demo.B"] },
                    { "title": "边界", "body": "图里到这里为止。", "citedUids": [] }
                  ],
                  "sessionExpired": false,
                  "citations": [],
                  "uiCommands": [],
                  "suggestions": [],
                  "steps": [],
                  "meta": {
                    "intent": "EXPLORE_FLOW",
                    "confidence": 0.9,
                    "graphRev": 1,
                    "latencyMs": 42
                  }
                }
                """;

        AgentResponse response = mapper.readValue(json, AgentResponse.class);

        assertEquals(2, response.getSections().size(), "sections 字段被静默丢了");
        assertEquals("A 只调用了 B", response.getHeadline());
        AgentResponse.Section first = response.getSections().get(0);
        assertEquals("路径", first.getTitle());
        assertEquals("`A` 调用 `B`。", first.getBody());
        assertEquals(List.of("com.demo.A", "com.demo.B"), first.getCitedUids());
        assertTrue(response.getSections().get(1).getCitedUids().isEmpty(),
                "空 citedUids 必须原样保留 —— 它是「这段没有出处」的语义");

        String out = mapper.writeValueAsString(response);
        assertTrue(out.contains("\"sections\""), "序列化时漏了 sections —— 前端收不到");
        assertTrue(out.contains("\"citedUids\""), "子字段名必须是驼峰 citedUids");
    }
}
