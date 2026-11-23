package com.example.archmind.service.domain;

import java.util.List;

/**
 * 对账结果。ok 为 false 时 failures 列出全部具体原因；
 * warnings 是"不规范但可以落库"的情况，例如某个业务领域没有任何入口能力。
 */
public record ValidationReport(boolean ok, List<String> failures, List<String> warnings) {

    public ValidationReport(boolean ok, List<String> failures) {
        this(ok, failures, List.of());
    }
}
