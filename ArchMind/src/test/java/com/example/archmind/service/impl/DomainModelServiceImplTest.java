package com.example.archmind.service.impl;

import com.example.archmind.common.exception.BusinessException;
import com.example.archmind.dao.ProjectMapper;
import com.example.archmind.service.domain.BusinessArea;
import com.example.archmind.service.domain.BusinessAreaResolver;
import com.example.archmind.service.domain.Capability;
import com.example.archmind.service.domain.CapabilityResolver;
import com.example.archmind.service.domain.DomainModelValidator;
import com.example.archmind.service.domain.DomainModelWriter;
import com.example.archmind.service.domain.GraphSnapshot;
import com.example.archmind.service.domain.GraphSnapshotDao;
import com.example.archmind.service.domain.Role;
import com.example.archmind.service.domain.RoleClassifier;
import com.example.archmind.service.domain.ValidationReport;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.InjectMocks;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * DomainModelServiceImpl 单测：用一个类、一个入口的最小快照串起
 * load → classify → resolve 领域 → resolve 能力 → validate → write 的编排。
 */
@ExtendWith(MockitoExtension.class)
class DomainModelServiceImplTest {

    @Mock
    private GraphSnapshotDao snapshotDao;
    @Mock
    private RoleClassifier roleClassifier;
    @Mock
    private BusinessAreaResolver areaResolver;
    @Mock
    private CapabilityResolver capabilityResolver;
    @Mock
    private DomainModelValidator validator;
    @Mock
    private DomainModelWriter writer;
    @Mock
    private ProjectMapper projectMapper;

    @InjectMocks
    private DomainModelServiceImpl service;

    private GraphSnapshot stubSnapshotAndResolvers() {
        GraphSnapshot.ClassSnapshot cls = new GraphSnapshot.ClassSnapshot(
                "a.A", "A", "a", List.of(), List.of(), "");
        GraphSnapshot snapshot = new GraphSnapshot(
                List.of(cls),
                List.of(),
                List.of(new GraphSnapshot.EndpointSnapshot("GET /a", "http", "a.A#x()")));
        Map<String, RoleClassifier.Classified> roles =
                Map.of("a.A", new RoleClassifier.Classified("a.A", Role.BUSINESS, "package"));

        when(snapshotDao.load(1L)).thenReturn(snapshot);
        when(roleClassifier.classify(snapshot.classes())).thenReturn(roles);
        when(projectMapper.selectById(1L)).thenReturn(null);
        when(areaResolver.resolve(snapshot.classes(), roles, "project-1",
                snapshot.endpoints(), snapshot.calls()))
                .thenReturn(new BusinessAreaResolver.AreaResult(
                        List.of(new BusinessArea("a", "a", "business")),
                        Map.of("a.A", "a"), "single"));
        when(capabilityResolver.resolve(snapshot.endpoints(), Map.of("a.A", "a"), roles, snapshot.calls()))
                .thenReturn(new Capability.CapabilityResult(
                        List.of(new Capability("GET /a", "GET /a", "a", "a.A#x()",
                                List.of(new Capability.Member("a.A", 0)))),
                        List.of(), List.of()));
        return snapshot;
    }

    @Test
    void buildWritesModelWhenValidationPasses() {
        stubSnapshotAndResolvers();
        when(validator.validate(any(), eq(1))).thenReturn(new ValidationReport(true, List.of()));

        List<Integer> progress = new java.util.ArrayList<>();
        service.build(1L, progress::add);

        verify(writer).write(eq(1L), any());
        assertEquals(List.of(0, 20, 40, 60, 80, 95, 100), progress);
    }

    @Test
    void buildThrowsWhenValidationFails() {
        stubSnapshotAndResolvers();
        when(validator.validate(any(), eq(1)))
                .thenReturn(new ValidationReport(false, List.of("BELONGS_TO: a.A")));

        BusinessException ex = assertThrows(BusinessException.class, () -> service.build(1L, pct -> {}));

        assertTrue(ex.getMessage().contains("业务建模对账失败"));
        assertTrue(ex.getMessage().contains("BELONGS_TO: a.A"));
        verify(writer, never()).write(anyLong(), any());
    }
}
