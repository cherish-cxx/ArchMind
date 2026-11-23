package com.example.archmind.service.impl;

import com.example.archmind.service.impl.DomainDescriptionServiceImpl.AreaDesc;
import com.example.archmind.service.impl.DomainDescriptionServiceImpl.CapDesc;
import com.example.archmind.service.impl.DomainDescriptionServiceImpl.DomainDescriptionBatch;
import org.junit.jupiter.api.Test;
import org.neo4j.driver.Driver;
import org.springframework.ai.chat.model.ChatModel;

import java.util.List;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.Mockito.mock;

/**
 * DomainDescriptionServiceImpl 的可测缝隙：解析与行过滤都是纯函数，
 * 用 mock 的 ChatModel/Driver 构造 service，不连库、不触发网络。
 */
class DomainDescriptionServiceImplTest {

    private final DomainDescriptionServiceImpl service =
            new DomainDescriptionServiceImpl(mock(ChatModel.class), mock(Driver.class));

    @Test
    void parseDropsBlankDescriptions() {
        String json = """
            {"areas":[{"uid":"item","description":"负责商品查询与上架。"},
                      {"uid":"cart","description":"  "}],
             "capabilities":[{"uid":"GET /items","description":"按分类查商品。"}]}
            """;
        DomainDescriptionBatch b = service.parse(json);
        assertEquals(List.of("item"), b.areas().stream().map(AreaDesc::uid).toList());
        assertEquals(1, b.capabilities().size());
    }

    @Test
    void rowsOnlyKeepKnownUids() {
        DomainDescriptionBatch b = new DomainDescriptionBatch(
                List.of(new AreaDesc("item", "商品"), new AreaDesc("ghost", "编的")),
                List.of(new CapDesc("GET /items", "查商品"), new CapDesc("GET /nope", "编的")));

        var areaRows = service.areaRows(b, Set.of("item"));
        var capRows = service.capabilityRows(b, Set.of("GET /items"));

        assertEquals(1, areaRows.size());
        assertEquals("商品", areaRows.get(0).get("description"));
        assertEquals(1, capRows.size());
    }
}
