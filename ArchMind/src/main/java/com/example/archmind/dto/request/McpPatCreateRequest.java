package com.example.archmind.dto.request;

import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;
import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
public class McpPatCreateRequest {

    @NotBlank(message = "token 名称不能为空")
    @Size(max = 64, message = "token 名称不能超过 64 字符")
    private String name;

    /** 1..365；为空默认 90 天 */
    @Min(value = 1, message = "有效期至少 1 天")
    @Max(value = 365, message = "有效期最多 365 天")
    private Integer expiresInDays;
}
