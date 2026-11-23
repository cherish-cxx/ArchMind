package com.example.archmind.service.domain;

import java.util.List;

/**
 * 能力：一个入口 + 它真正执行到的类。能力与入口 1:1，uid 即入口 uid。
 *
 * <p>{@link Member} 是能力成员类及其从入口出发的最短跳数；
 * {@link AreaCall} 是跨业务领域调用的断点；{@link CapabilityResult} 汇总全部能力的生成结果。
 */
public record Capability(String uid, String name, String areaUid, String entryUid,
                         List<Member> members) {

    /** 能力成员：类 uid 与从入口方法出发的最短跳数（入口类为 0）。 */
    public record Member(String classUid, int depth) {
    }

    /** 跨领域调用：入口能力停在另一个业务领域，记录来源与目标类。 */
    public record AreaCall(String fromAreaUid, String toAreaUid,
                           String fromClassUid, String toClassUid) {
    }

    /** 能力生成结果；gaps 记录外部依赖等无法继续展开的断点。 */
    public record CapabilityResult(List<Capability> capabilities,
                                   List<AreaCall> areaCalls,
                                   List<String> gaps) {
    }
}
