package com.example.archmind.service.domain;

import com.example.archmind.service.domain.Capability.AreaCall;
import com.example.archmind.service.domain.Capability.Member;
import com.example.archmind.service.domain.RoleClassifier.Classified;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class DomainModelWriterTest {

    @Test
    void planEmitsClassAreaRoleAndMemberRows() {
        var writer = new DomainModelWriter(null);
        var plan = writer.plan(1L, consistentModel());
        assertEquals(1, plan.areas().size());
        assertEquals(1, plan.classAreas().size());
        assertEquals("annotation", plan.roles().get(0).get("basis"));
        assertEquals(0, plan.members().get(0).get("depth"));
    }

    @Test
    void writeOrderDeletesBeforeMerge() {
        assertTrue(DomainModelWriter.WRITE_ORDER.get(0).contains("DETACH DELETE"));
        assertTrue(DomainModelWriter.WRITE_ORDER.get(1).contains("DETACH DELETE"));
    }

    @Test
    void areaCallStatementKeepsSourceClasses() {
        String statement = DomainModelWriter.WRITE_ORDER.get(DomainModelWriter.WRITE_ORDER.size() - 1);
        assertTrue(statement.contains("fromClassUids"));
    }

    private static DomainModel consistentModel() {
        return new DomainModel(
                List.of(new BusinessArea("item", "item", "business")),
                List.of(new Capability("POST /i", "POST /i", "item", "a.C#x()",
                        List.of(new Member("a.C", 0)))),
                List.of(new AreaCall("item", "infrastructure", "a.C", "a.D")),
                Map.of("a.C", "item"),
                Map.of("a.C", new Classified("a.C", Role.ENTRY, "annotation")));
    }
}
