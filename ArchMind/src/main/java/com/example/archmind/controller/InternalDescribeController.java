package com.example.archmind.controller;

import com.example.archmind.common.result.Result;
import com.example.archmind.common.util.CheckProjectUtil;
import com.example.archmind.dto.request.DescribePendingRequest;
import com.example.archmind.dto.response.DescribeBatchResult;
import com.example.archmind.dto.response.DescriptionStatusResponse;
import com.example.archmind.service.ClassDescriptionService;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * 类描述补齐的内部接口，走 {@code /internal/**} 的共享密钥鉴权。
 *
 * <p>一次请求只处理一批，驱动侧循环调用直到 {@code remaining == 0}。
 * blocked / busy 时 remaining 为 null，表示「不知道还剩多少」，不能当作已完成。</p>
 */
@RestController
@RequestMapping("/internal/describe")
@RequiredArgsConstructor
public class InternalDescribeController {

    private final ClassDescriptionService classDescriptionService;
    private final CheckProjectUtil checkProjectUtil;

    @PostMapping("/pending")
    public Result<DescribeBatchResult> pending(@RequestBody DescribePendingRequest request) {
        checkProjectUtil.checkProjectExists(request.getProjectId());
        int limit = request.getLimit() == null ? 0 : request.getLimit();
        return Result.success(classDescriptionService.describePending(request.getProjectId(), limit));
    }

    @GetMapping("/status")
    public Result<DescriptionStatusResponse> status(@RequestParam Long projectId) {
        checkProjectUtil.checkProjectExists(projectId);
        return Result.success(classDescriptionService.descriptionStatus(projectId));
    }
}
