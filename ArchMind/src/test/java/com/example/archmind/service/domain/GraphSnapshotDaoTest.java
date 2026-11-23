package com.example.archmind.service.domain;

import com.example.archmind.service.domain.GraphSnapshot.ClassSnapshot;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;

class GraphSnapshotDaoTest {

    @Test
    void assembleToleratesMissingProperties() {
        List<Map<String, Object>> classRows =
                List.of(Map.of("uid", "a.A", "name", "A", "packageName", "a"));
        var s = GraphSnapshotDao.assemble(classRows, List.of(), List.of());
        assertEquals(1, s.classes().size());
        assertEquals(List.of(), s.classes().get(0).annotationNames());
        assertEquals("", s.classes().get(0).superclass());
    }

    @Test
    void assembleKeepsOrderAndShapesCalls() {
        List<Map<String, Object>> classRows = List.of(
                Map.of("uid", "a.B", "name", "B", "packageName", "a"),
                Map.of("uid", "a.A", "name", "A", "packageName", "a"));
        List<Map<String, Object>> callRows =
                List.of(Map.of("from", "a.A#x()", "to", "a.B#y()"));
        var s = GraphSnapshotDao.assemble(classRows, callRows, List.of());
        assertEquals(List.of("a.A", "a.B"), s.classes().stream().map(ClassSnapshot::uid).toList());
        assertEquals("a.B#y()", s.calls().get(0).toMethodUid());
    }
}
