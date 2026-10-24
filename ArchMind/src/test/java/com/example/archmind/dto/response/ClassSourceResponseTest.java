package com.example.archmind.dto.response;

import org.junit.jupiter.api.Test;
import tools.jackson.databind.ObjectMapper;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 方法大纲的契约测试：前端按这些字段名渲染目录，改名就会静默消失。
 *
 * <p>与 {@link AgentResponseSectionsTest} 同一条理由 —— Spring Boot 默认忽略未知字段，
 * DTO 少一个 getter，Python/前端那边只会「数据不见了」，不会报错。</p>
 */
class ClassSourceResponseTest {

    private final ObjectMapper mapper = new ObjectMapper();

    @Test
    void methodOutlineIsSerializedWithExpectedNames() throws Exception {
        ClassSourceResponse.MethodOutline m = new ClassSourceResponse.MethodOutline();
        m.setUid("com.demo.OrderServiceImpl#createOrder(OrderFormDTO)");
        m.setName("createOrder");
        m.setSignature("createOrder(OrderFormDTO)");
        m.setReturnType("OrderVO");
        m.setVisibility("public");
        m.setStartLine(120);
        m.setEndLine(186);
        m.setConstructor(false);

        ClassSourceResponse resp = new ClassSourceResponse();
        resp.setUid("com.demo.OrderServiceImpl");
        resp.setContent("class OrderServiceImpl {}");
        resp.setMethods(List.of(m));

        String json = mapper.writeValueAsString(resp);

        assertTrue(json.contains("\"methods\""), "方法大纲字段丢了");
        for (String key : List.of("\"uid\"", "\"name\"", "\"signature\"", "\"returnType\"",
                "\"visibility\"", "\"startLine\"", "\"endLine\"", "\"constructor\"")) {
            assertTrue(json.contains(key), "大纲字段缺了: " + key);
        }
    }
}
