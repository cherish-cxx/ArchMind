package com.example.archmind.service.domain;

import java.util.List;

/** 对账结果；ok 为 false 时 failures 列出全部具体原因。 */
public record ValidationReport(boolean ok, List<String> failures) {
}
