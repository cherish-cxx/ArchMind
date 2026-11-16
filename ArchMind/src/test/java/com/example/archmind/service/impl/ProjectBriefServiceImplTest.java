package com.example.archmind.service.impl;

import com.example.archmind.common.exception.BusinessException;
import com.example.archmind.dao.ProjectMapper;
import com.example.archmind.dto.response.BriefResponse;
import com.example.archmind.dto.response.ProjectOverviewResponse;
import com.example.archmind.entity.Project;
import com.example.archmind.service.ProjectOverviewService;
import com.example.archmind.service.ast.GraphRevService;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.Mockito.when;

@ExtendWith(MockitoExtension.class)
class ProjectBriefServiceImplTest {

    private static final String MCP_PUBLIC_URL = "https://archmind.test/mcp";

    @Mock
    private ProjectMapper projectMapper;
    @Mock
    private ProjectOverviewService overviewService;
    @Mock
    private GraphRevService graphRevService;

    private ProjectBriefServiceImpl service;

    @BeforeEach
    void setUp() {
        service = new ProjectBriefServiceImpl(
                projectMapper, overviewService, graphRevService, MCP_PUBLIC_URL);
    }

    private ProjectOverviewResponse sampleOverview() {
        ProjectOverviewResponse overview = new ProjectOverviewResponse();
        overview.setProjectType("Spring Boot 电商系统");
        overview.setSummary("电商交易系统");

        ProjectOverviewResponse.TechStackItem tech = new ProjectOverviewResponse.TechStackItem();
        tech.setName("Spring Boot");
        tech.setCategory("后端框架");
        overview.setTechStack(List.of(tech));

        ProjectOverviewResponse.Architecture architecture = new ProjectOverviewResponse.Architecture();
        architecture.setStyle("分层架构");
        architecture.setLayers(List.of("controller", "service", "mapper"));
        overview.setArchitecture(architecture);

        ProjectOverviewResponse.ModuleItem module = new ProjectOverviewResponse.ModuleItem();
        module.setName("订单");
        module.setResponsibility("下单与库存扣减");
        overview.setModules(List.of(module));
        return overview;
    }

    private void stubProject(Long projectId) {
        Project project = new Project();
        project.setId(projectId);
        project.setName("hmall");
        when(projectMapper.selectById(projectId)).thenReturn(project);
    }

    @Test
    void rendersProjectMapWithProjectIdAndNoUserIdentity() {
        stubProject(99L);
        when(overviewService.tryGetExistingOverview(99L)).thenReturn(sampleOverview());
        when(graphRevService.currentRev(99L)).thenReturn(7L);

        BriefResponse res = service.render(99L, "claude");

        assertEquals("CLAUDE.md", res.getFilename());
        assertEquals(7L, res.getGraphRev());
        assertTrue(res.getContent().startsWith("# CLAUDE.md"));
        assertTrue(res.getContent().contains("> 由 ArchMind 生成"));
        assertTrue(res.getContent().contains("生成时间"));
        assertTrue(res.getContent().contains("projectId: 99"));
        assertTrue(res.getContent().contains("hmall"));
        assertTrue(res.getContent().contains("电商交易系统"));
        assertTrue(res.getContent().contains("## 技术栈"));
        assertTrue(res.getContent().contains("Spring Boot"));
        assertTrue(res.getContent().contains("## 架构"));
        assertTrue(res.getContent().contains("## 模块"));
        assertTrue(res.getContent().contains("订单"));
        assertTrue(res.getContent().contains("## 使用 ArchMind MCP"));
        assertTrue(res.getContent().contains("archmind_list_projects"));
        assertTrue(res.getContent().contains("archmind_overview"));
        assertTrue(res.getContent().contains("archmind_search_entities"));
        assertTrue(res.getContent().contains("archmind_trace_flow"));
        assertTrue(res.getContent().contains("archmind_project_brief"));
        assertTrue(res.getContent().contains("graphRev=7"));
        assertTrue(res.getContent().contains("Authorization: Bearer $ARCHMIND_PAT"));
        assertTrue(res.getContent().contains("MCP 端点：`" + MCP_PUBLIC_URL + "`"));
        assertTrue(res.getContent().contains("所有 ArchMind MCP 工具调用都使用 projectId: 99"));
        assertFalse(res.getContent().contains("userId"));
    }

    @Test
    void rendersAgentsFilenameAndTitle() {
        stubProject(99L);
        when(overviewService.tryGetExistingOverview(99L)).thenReturn(sampleOverview());
        when(graphRevService.currentRev(99L)).thenReturn(0L);

        BriefResponse res = service.render(99L, "agents");

        assertEquals("AGENTS.md", res.getFilename());
        assertTrue(res.getContent().startsWith("# AGENTS.md"));
        assertTrue(res.getContent().contains("projectId: 99"));
    }

    @Test
    void rejectsUnknownTarget() {
        assertThrows(BusinessException.class, () -> service.render(99L, "readme"));
    }

    @Test
    void missingOverviewThrows() {
        stubProject(99L);
        when(overviewService.tryGetExistingOverview(99L)).thenReturn(null);

        BusinessException ex = assertThrows(BusinessException.class,
                () -> service.render(99L, "claude"));
        assertEquals("该项目尚未生成项目概况，请先完成分析", ex.getMessage());
    }

    @Test
    void nullAndEmptyOverviewFieldsRenderWithoutNpe() {
        stubProject(99L);
        ProjectOverviewResponse empty = new ProjectOverviewResponse();
        when(overviewService.tryGetExistingOverview(99L)).thenReturn(empty);
        when(graphRevService.currentRev(99L)).thenReturn(3L);

        BriefResponse res = service.render(99L, "claude");

        assertTrue(res.getContent().contains("## 技术栈"));
        assertTrue(res.getContent().contains("## 架构"));
        assertTrue(res.getContent().contains("## 模块"));
        assertTrue(res.getContent().contains("（暂无）"));
        assertTrue(res.getContent().contains("graphRev=3"));
    }

    @Test
    void contentNeverContainsUserIdOrToken() {
        stubProject(99L);
        ProjectOverviewResponse overview = sampleOverview();
        overview.setDescription("含内部描述");
        when(overviewService.tryGetExistingOverview(99L)).thenReturn(overview);
        when(graphRevService.currentRev(99L)).thenReturn(7L);

        BriefResponse res = service.render(99L, "claude");

        assertFalse(res.getContent().contains("userId"));
        assertFalse(res.getContent().contains("am_pat_"));
    }
}
