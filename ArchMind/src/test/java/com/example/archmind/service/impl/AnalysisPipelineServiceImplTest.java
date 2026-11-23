package com.example.archmind.service.impl;

import com.example.archmind.service.ClassDescriptionService;
import com.example.archmind.service.ProgressReporter;
import com.example.archmind.service.ProjectOverviewService;
import com.example.archmind.service.ast.CodeGraphService;
import com.example.archmind.service.domain.DomainModelService;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.InjectMocks;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.util.ArrayList;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.verify;

/**
 * AnalysisPipelineServiceImpl 单测：只验证阶段编排与进度上报，不起 Spring、不连真库。
 */
@ExtendWith(MockitoExtension.class)
class AnalysisPipelineServiceImplTest {

    @Mock
    private ProjectOverviewService projectOverviewService;
    @Mock
    private CodeGraphService codeGraphService;
    @Mock
    private ClassDescriptionService classDescriptionService;
    @Mock
    private DomainModelService domainModelService;

    @InjectMocks
    private AnalysisPipelineServiceImpl pipeline;

    @Test
    void domainStageRunsBuildAndReportsFull() {
        RecordingReporter reporter = new RecordingReporter();

        pipeline.domainStage(1L, reporter);

        verify(domainModelService).build(eq(1L), any());
        assertEquals(100, reporter.last());
    }

    /** 记录每次上报的阶段内进度，便于断言最后一个值。 */
    private static final class RecordingReporter implements ProgressReporter {
        private final List<Integer> reports = new ArrayList<>();

        @Override
        public void report(int percentWithinStage) {
            reports.add(percentWithinStage);
        }

        int last() {
            return reports.get(reports.size() - 1);
        }
    }
}
