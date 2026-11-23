package com.example.archmind.service.domain;

import com.example.archmind.service.domain.Capability.Member;
import com.example.archmind.service.domain.RoleClassifier.Classified;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class DomainModelValidatorTest {

    @Test
    void failsWhenAClassHasNoArea() {
        var model = new DomainModel(List.of(new BusinessArea("item", "item", "business")),
                List.of(), List.of(), Map.of("a.A", "item"),
                Map.of("a.A", new Classified("a.A", Role.BUSINESS, "package")));
        var r = new DomainModelValidator().validate(model, 2);
        assertFalse(r.ok());
        assertTrue(r.failures().stream().anyMatch(f -> f.contains("BELONGS_TO")));
    }

    @Test
    void warnsButAllowsBusinessAreaWithoutCapability() {
        var model = new DomainModel(
                List.of(new BusinessArea("item", "item", "business")),
                List.of(), List.of(),
                Map.of("a.A", "item"),
                Map.of("a.A", new Classified("a.A", Role.BUSINESS, "package")));
        var r = new DomainModelValidator().validate(model, 1);
        assertTrue(r.ok(), "领域没有能力只是警告，不应阻断落库");
        assertTrue(r.warnings().stream().anyMatch(w -> w.contains("capability")));
    }

    @Test
    void failsOnAreaWithoutClasses() {
        var model = new DomainModel(
                List.of(new BusinessArea("item", "item", "business"),
                        new BusinessArea("ghost", "ghost", "business")),
                List.of(new Capability("POST /i", "POST /i", "item", "a.C#x()",
                        List.of(new Member("a.C", 0)))),
                List.of(), Map.of("a.C", "item"),
                Map.of("a.C", new Classified("a.C", Role.ENTRY, "annotation")));
        var r = new DomainModelValidator().validate(model, 1);
        assertFalse(r.ok());
        assertTrue(r.failures().stream().anyMatch(f -> f.contains("empty area ghost")));
    }

    @Test
    void passesOnConsistentModel() {
        var model = new DomainModel(
                List.of(new BusinessArea("item", "item", "business")),
                List.of(new Capability("POST /i", "POST /i", "item", "a.C#x()",
                        List.of(new Member("a.C", 0)))),
                List.of(), Map.of("a.C", "item"),
                Map.of("a.C", new Classified("a.C", Role.ENTRY, "annotation")));
        assertTrue(new DomainModelValidator().validate(model, 1).ok());
    }
}
