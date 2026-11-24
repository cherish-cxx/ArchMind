package com.example.archmind.service.impl;

import com.example.archmind.common.exception.BusinessException;
import com.example.archmind.dao.ProjectMapper;
import com.example.archmind.entity.Project;
import com.example.archmind.service.ProgressReporter;
import com.example.archmind.service.domain.BusinessAreaResolver;
import com.example.archmind.service.domain.Capability;
import com.example.archmind.service.domain.CapabilityResolver;
import com.example.archmind.service.domain.DomainModel;
import com.example.archmind.service.domain.DomainModelService;
import com.example.archmind.service.domain.DomainModelValidator;
import com.example.archmind.service.domain.DomainModelWriter;
import com.example.archmind.service.domain.GraphSnapshot;
import com.example.archmind.service.domain.GraphSnapshotDao;
import com.example.archmind.service.domain.RoleClassifier;
import com.example.archmind.service.domain.ValidationReport;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;

import java.util.Map;

/**
 * 业务建模编排：只负责按顺序调用纯函数组件并上报进度，不包含任何算法。
 *
 * <p>对账失败时抛 {@link BusinessException} 且不落库，任务随之落 FAILED——不写半成品。
 * 生产装配只需三个 Spring bean；role / 领域 / 能力 / 校验四个组件是无状态纯函数，
 * 由本类直接构造，同时保留接收它们的构造函数供单测注入 mock。
 */
@Service
public class DomainModelServiceImpl implements DomainModelService {

    private static final org.slf4j.Logger log =
            org.slf4j.LoggerFactory.getLogger(DomainModelServiceImpl.class);

    private final GraphSnapshotDao snapshotDao;
    private final RoleClassifier roleClassifier;
    private final BusinessAreaResolver areaResolver;
    private final CapabilityResolver capabilityResolver;
    private final DomainModelValidator validator;
    private final DomainModelWriter writer;
    private final ProjectMapper projectMapper;

    @Autowired
    public DomainModelServiceImpl(GraphSnapshotDao snapshotDao, ProjectMapper projectMapper,
                                  DomainModelWriter writer) {
        this(snapshotDao, new RoleClassifier(), new BusinessAreaResolver(), new CapabilityResolver(),
                new DomainModelValidator(), writer, projectMapper);
    }

    DomainModelServiceImpl(GraphSnapshotDao snapshotDao, RoleClassifier roleClassifier,
                           BusinessAreaResolver areaResolver, CapabilityResolver capabilityResolver,
                           DomainModelValidator validator, DomainModelWriter writer,
                           ProjectMapper projectMapper) {
        this.snapshotDao = snapshotDao;
        this.roleClassifier = roleClassifier;
        this.areaResolver = areaResolver;
        this.capabilityResolver = capabilityResolver;
        this.validator = validator;
        this.writer = writer;
        this.projectMapper = projectMapper;
    }

    @Override
    public void build(Long projectId, ProgressReporter reporter) {
        reporter.report(0);
        GraphSnapshot snapshot = snapshotDao.load(projectId);
        reporter.report(20);

        Map<String, RoleClassifier.Classified> roles = roleClassifier.classify(snapshot.classes());
        reporter.report(40);

        BusinessAreaResolver.AreaResult areaResult =
                areaResolver.resolve(snapshot.classes(), roles, projectName(projectId),
                        snapshot.endpoints(), snapshot.calls());
        reporter.report(60);

        Capability.CapabilityResult capabilityResult = capabilityResolver.resolve(
                snapshot.endpoints(), areaResult.classToArea(), roles, snapshot.calls());
        reporter.report(80);

        DomainModel model = new DomainModel(
                areaResult.areas(), capabilityResult.capabilities(), capabilityResult.areaCalls(),
                areaResult.classToArea(), roles);

        ValidationReport report = validator.validate(model, snapshot.classes().size());
        reporter.report(95);
        if (!report.ok()) {
            throw new BusinessException("业务建模对账失败: " + String.join("; ", report.failures()));
        }
        if (!report.warnings().isEmpty()) {
            log.warn("业务建模警告 projectId={}: {}", projectId, report.warnings());
        }

        writer.write(projectId, model);
        reporter.report(100);
    }

    /** 项目名只用于单领域兜底命名；查不到或为空时退化成 project-{id}。 */
    private String projectName(Long projectId) {
        Project project = projectMapper.selectById(projectId);
        if (project == null || project.getName() == null || project.getName().isBlank()) {
            return "project-" + projectId;
        }
        return project.getName();
    }
}
