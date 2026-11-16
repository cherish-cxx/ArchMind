package com.example.archmind.service.impl;

import com.example.archmind.common.exception.McpAuthException;
import com.example.archmind.dao.McpPatMapper;
import com.example.archmind.dao.ProjectMapper;
import com.example.archmind.dto.request.McpPatCreateRequest;
import com.example.archmind.dto.response.McpContext;
import com.example.archmind.dto.response.McpPatCreatedResponse;
import com.example.archmind.dto.response.McpPrincipal;
import com.example.archmind.entity.McpPat;
import com.example.archmind.entity.Project;
import com.example.archmind.service.ast.GraphRevService;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.mockito.junit.jupiter.MockitoSettings;
import org.mockito.quality.Strictness;

import java.time.LocalDateTime;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
class McpPatServiceImplTest {

    @Mock
    private McpPatMapper mapper;
    @Mock
    private ProjectMapper projectMapper;
    @Mock
    private GraphRevService graphRevService;

    private McpPatServiceImpl service;

    @BeforeEach
    void setUp() {
        service = new McpPatServiceImpl(mapper, projectMapper, graphRevService);
    }

    @Test
    void createStoresOnlyHashAndReturnsPlaintextOnce() {
        when(mapper.insert(any(McpPat.class))).thenReturn(1);
        McpPatCreateRequest req = new McpPatCreateRequest();
        req.setName("claude-code");

        McpPatCreatedResponse res = service.create(7L, req);

        assertTrue(res.getToken().startsWith("am_pat_"));
        ArgumentCaptor<McpPat> captor = ArgumentCaptor.forClass(McpPat.class);
        verify(mapper).insert(captor.capture());
        assertEquals(64, captor.getValue().getTokenHash().length());
        assertNotEquals(res.getToken(), captor.getValue().getTokenHash());
        assertEquals(res.getToken().substring(0, 15), captor.getValue().getPrefix());
        assertNotNull(captor.getValue().getCreateTime());
        assertNotNull(captor.getValue().getExpiresAt());
        assertEquals(7L, captor.getValue().getUserId());
    }

    @Test
    void createDefaultsExpiryTo90DaysWhenNull() {
        when(mapper.insert(any(McpPat.class))).thenReturn(1);
        McpPatCreateRequest req = new McpPatCreateRequest();
        req.setName("claude-code");

        LocalDateTime before = LocalDateTime.now();
        service.create(7L, req);
        LocalDateTime after = LocalDateTime.now();

        ArgumentCaptor<McpPat> captor = ArgumentCaptor.forClass(McpPat.class);
        verify(mapper).insert(captor.capture());
        LocalDateTime expiresAt = captor.getValue().getExpiresAt();
        assertFalse(expiresAt.isBefore(before.plusDays(90)));
        assertFalse(expiresAt.isAfter(after.plusDays(90)));
    }

    @Test
    void createRespectsExplicitExpiry() {
        when(mapper.insert(any(McpPat.class))).thenReturn(1);
        McpPatCreateRequest req = new McpPatCreateRequest();
        req.setName("claude-code");
        req.setExpiresInDays(30);

        LocalDateTime before = LocalDateTime.now();
        service.create(7L, req);
        LocalDateTime after = LocalDateTime.now();

        ArgumentCaptor<McpPat> captor = ArgumentCaptor.forClass(McpPat.class);
        verify(mapper).insert(captor.capture());
        LocalDateTime expiresAt = captor.getValue().getExpiresAt();
        assertFalse(expiresAt.isBefore(before.plusDays(30)));
        assertFalse(expiresAt.isAfter(after.plusDays(30)));
    }

    @Test
    void authenticateRejectsUnknownToken() {
        when(mapper.selectOne(any())).thenReturn(null);

        McpAuthException ex = assertThrows(McpAuthException.class,
                () -> service.authenticate("am_pat_unknown"));
        assertEquals(401, ex.getStatus());
    }

    @Test
    void authenticateRejectsExpiredToken() {
        McpPat pat = pat(1L, 7L, null, LocalDateTime.now().minusDays(1), null);
        when(mapper.selectOne(any())).thenReturn(pat);

        McpAuthException ex = assertThrows(McpAuthException.class,
                () -> service.authenticate("am_pat_expired"));
        assertEquals(401, ex.getStatus());
        verify(mapper, never()).updateById(any(McpPat.class));
    }

    @Test
    void authenticateRejectsRevokedToken() {
        McpPat pat = pat(1L, 7L, null, LocalDateTime.now().plusDays(30), LocalDateTime.now().minusHours(1));
        when(mapper.selectOne(any())).thenReturn(pat);

        McpAuthException ex = assertThrows(McpAuthException.class,
                () -> service.authenticate("am_pat_revoked"));
        assertEquals(401, ex.getStatus());
        verify(mapper, never()).updateById(any(McpPat.class));
    }

    @Test
    void authenticateUpdatesLastUsedAt() {
        McpPat pat = pat(5L, 7L, 99L, LocalDateTime.now().plusDays(30), null);
        when(mapper.selectOne(any())).thenReturn(pat);

        McpPrincipal principal = service.authenticate("am_pat_valid");

        assertEquals(5L, principal.getPatId());
        assertEquals(7L, principal.getUserId());
        assertEquals(99L, principal.getBoundProjectId());
        ArgumentCaptor<McpPat> captor = ArgumentCaptor.forClass(McpPat.class);
        verify(mapper).updateById(captor.capture());
        assertNotNull(captor.getValue().getLastUsedAt());
    }

    @Test
    void contextRejectsForeignProject() {
        McpPrincipal principal = new McpPrincipal(1L, 7L, null, null);
        Project p = new Project();
        p.setId(99L);
        p.setUserId(8L);
        when(projectMapper.selectById(99L)).thenReturn(p);

        McpAuthException ex = assertThrows(McpAuthException.class,
                () -> service.context(principal, 99L));
        assertEquals(403, ex.getStatus());
    }

    @Test
    void contextReturns404ForMissingProject() {
        McpPrincipal principal = new McpPrincipal(1L, 7L, null, null);
        when(projectMapper.selectById(99L)).thenReturn(null);

        McpAuthException ex = assertThrows(McpAuthException.class,
                () -> service.context(principal, 99L));
        assertEquals(404, ex.getStatus());
    }

    @Test
    void contextRejectsBoundProjectMismatch() {
        McpPrincipal principal = new McpPrincipal(1L, 7L, 5L, null);
        Project p = new Project();
        p.setId(99L);
        p.setUserId(7L);
        when(projectMapper.selectById(99L)).thenReturn(p);

        McpAuthException ex = assertThrows(McpAuthException.class,
                () -> service.context(principal, 99L));
        assertEquals(403, ex.getStatus());
    }

    @Test
    void contextFillsGraphRev() {
        McpPrincipal principal = new McpPrincipal(1L, 7L, null, null);
        Project p = new Project();
        p.setId(99L);
        p.setUserId(7L);
        p.setName("hmall");
        p.setAnalysisStatus("COMPLETED");
        when(projectMapper.selectById(99L)).thenReturn(p);
        when(graphRevService.currentRev(99L)).thenReturn(7L);

        McpContext ctx = service.context(principal, 99L);

        assertEquals(7L, ctx.getUserId());
        assertEquals(1L, ctx.getPatId());
        assertEquals(99L, ctx.getProjectId());
        assertEquals("hmall", ctx.getProjectName());
        assertEquals("COMPLETED", ctx.getAnalysisStatus());
        assertEquals(7L, ctx.getGraphRev());
    }

    private static McpPat pat(Long id, Long userId, Long projectId,
                              LocalDateTime expiresAt, LocalDateTime revokedAt) {
        McpPat pat = new McpPat();
        pat.setId(id);
        pat.setUserId(userId);
        pat.setProjectId(projectId);
        pat.setName("claude-code");
        pat.setTokenHash("hash");
        pat.setPrefix("am_pat_prefix");
        pat.setCreateTime(LocalDateTime.now().minusDays(1));
        pat.setExpiresAt(expiresAt);
        pat.setRevokedAt(revokedAt);
        return pat;
    }
}
