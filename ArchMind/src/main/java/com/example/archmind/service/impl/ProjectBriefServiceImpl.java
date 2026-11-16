package com.example.archmind.service.impl;

import com.example.archmind.common.exception.BusinessException;
import com.example.archmind.dao.ProjectMapper;
import com.example.archmind.dto.response.BriefResponse;
import com.example.archmind.dto.response.ProjectOverviewResponse;
import com.example.archmind.entity.Project;
import com.example.archmind.service.ProjectBriefService;
import com.example.archmind.service.ProjectOverviewService;
import com.example.archmind.service.ast.GraphRevService;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.util.List;

/**
 * 渲染 AGENTS.md / CLAUDE.md 草稿。
 *
 * <p>只输出项目认知与 {@code projectId}；不读取、不输出 userId 或任何 token。</p>
 */
@Service
public class ProjectBriefServiceImpl implements ProjectBriefService {

    private static final String EMPTY = "（暂无）";
    private static final DateTimeFormatter TIME_FORMAT =
            DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss");

    private static final List<String> TOOL_NAMES = List.of(
            "archmind_list_projects",
            "archmind_overview",
            "archmind_search_entities",
            "archmind_trace_flow",
            "archmind_project_brief");

    private final ProjectMapper projectMapper;
    private final ProjectOverviewService overviewService;
    private final GraphRevService graphRevService;
    private final String mcpPublicUrl;

    public ProjectBriefServiceImpl(ProjectMapper projectMapper,
                                   ProjectOverviewService overviewService,
                                   GraphRevService graphRevService,
                                   @Value("${mcp.public-url:${MCP_PUBLIC_URL:https://your-host/mcp}}")
                                   String mcpPublicUrl) {
        this.projectMapper = projectMapper;
        this.overviewService = overviewService;
        this.graphRevService = graphRevService;
        this.mcpPublicUrl = mcpPublicUrl;
    }

    @Override
    public BriefResponse render(Long projectId, String target) {
        String filename = filenameOf(target);

        Project project = projectMapper.selectById(projectId);
        if (project == null) {
            throw new BusinessException("项目不存在: " + projectId);
        }
        ProjectOverviewResponse overview = overviewService.tryGetExistingOverview(projectId);
        if (overview == null) {
            throw new BusinessException("该项目尚未生成项目概况，请先完成分析");
        }
        long graphRev = graphRevService.currentRev(projectId);

        String content = buildContent(project, overview, projectId, graphRev, filename);
        return BriefResponse.builder()
                .filename(filename)
                .content(content)
                .graphRev(graphRev)
                .build();
    }

    private String filenameOf(String target) {
        if ("claude".equals(target)) {
            return "CLAUDE.md";
        }
        if ("agents".equals(target)) {
            return "AGENTS.md";
        }
        throw new BusinessException("不支持的导出目标: " + target + "（仅支持 claude / agents）");
    }

    private String buildContent(Project project, ProjectOverviewResponse overview,
                                Long projectId, long graphRev, String filename) {
        StringBuilder sb = new StringBuilder();
        sb.append("# ").append(filename).append("\n\n");
        sb.append("> 由 ArchMind 生成，为草稿，请按需修改后再使用。\n\n");
        sb.append("- 生成时间：").append(LocalDateTime.now().format(TIME_FORMAT)).append('\n');
        sb.append("- projectId: ").append(projectId).append('\n');
        sb.append("- 项目名：").append(text(project.getName())).append('\n');
        sb.append("- 项目类型：").append(text(overview.getProjectType())).append('\n');
        sb.append("- 摘要：").append(text(overview.getSummary())).append('\n');
        sb.append("- graphRev=").append(graphRev).append("（图版本，图变更时递增）\n\n");

        sb.append("## 技术栈\n\n").append(renderTechStack(overview.getTechStack())).append("\n\n");
        sb.append("## 架构\n\n").append(renderArchitecture(overview.getArchitecture())).append("\n\n");
        sb.append("## 模块\n\n").append(renderModules(overview.getModules())).append("\n\n");
        sb.append("## 使用 ArchMind MCP\n\n");
        sb.append("本项目已通过 ArchMind MCP 暴露为只读工具。连接信息：\n\n");
        sb.append("- MCP 端点：`").append(mcpPublicUrl).append("`\n");
        sb.append("- 鉴权：请求头 `Authorization: Bearer $ARCHMIND_PAT`（PAT 在 ArchMind 网页创建）\n\n");
        sb.append("所有 ArchMind MCP 工具调用都使用 projectId: ").append(projectId).append("。\n\n");
        sb.append("可用工具：\n\n");
        for (String tool : TOOL_NAMES) {
            sb.append("- `").append(tool).append("`\n");
        }
        return sb.toString();
    }

    private String renderTechStack(List<ProjectOverviewResponse.TechStackItem> items) {
        if (items == null || items.isEmpty()) {
            return EMPTY;
        }
        StringBuilder sb = new StringBuilder();
        for (ProjectOverviewResponse.TechStackItem item : items) {
            if (item == null) {
                continue;
            }
            sb.append("- ").append(text(item.getName()));
            if (!isBlank(item.getCategory())) {
                sb.append("（").append(item.getCategory());
                if (!isBlank(item.getVersion())) {
                    sb.append(" ").append(item.getVersion());
                }
                sb.append("）");
            } else if (!isBlank(item.getVersion())) {
                sb.append("（").append(item.getVersion()).append("）");
            }
            if (!isBlank(item.getRole())) {
                sb.append("：").append(item.getRole());
            }
            sb.append('\n');
        }
        return sb.length() == 0 ? EMPTY : sb.toString().stripTrailing();
    }

    private String renderArchitecture(ProjectOverviewResponse.Architecture architecture) {
        if (architecture == null) {
            return EMPTY;
        }
        StringBuilder sb = new StringBuilder();
        sb.append("- 风格：").append(text(architecture.getStyle())).append('\n');
        List<String> layers = architecture.getLayers();
        String layerText = (layers == null || layers.isEmpty())
                ? EMPTY : String.join(" → ", layers);
        sb.append("- 分层：").append(layerText).append('\n');
        sb.append("- 说明：").append(text(architecture.getDescription()));
        return sb.toString();
    }

    private String renderModules(List<ProjectOverviewResponse.ModuleItem> modules) {
        if (modules == null || modules.isEmpty()) {
            return EMPTY;
        }
        StringBuilder sb = new StringBuilder();
        sb.append("| 模块 | 职责 | 关键文件 |\n");
        sb.append("| --- | --- | --- |\n");
        for (ProjectOverviewResponse.ModuleItem module : modules) {
            if (module == null) {
                continue;
            }
            List<String> keyFiles = module.getKeyFiles();
            String keyFileText = (keyFiles == null || keyFiles.isEmpty())
                    ? EMPTY : String.join("、", keyFiles);
            sb.append("| ").append(cell(module.getName()))
                    .append(" | ").append(cell(module.getResponsibility()))
                    .append(" | ").append(cell(keyFileText))
                    .append(" |\n");
        }
        return sb.toString().stripTrailing();
    }

    private static String cell(String value) {
        return text(value).replace("|", "\\|");
    }

    private static String text(String value) {
        return isBlank(value) ? EMPTY : value;
    }

    private static boolean isBlank(String value) {
        return value == null || value.isBlank();
    }
}
