package com.example.archmind.dto.response;

import lombok.AllArgsConstructor;
import lombok.Data;
import lombok.NoArgsConstructor;

/**
 * 创建 + 上传一体化的返回：project 一定有，upload 在幂等重放时可能为 null。
 */
@Data
@NoArgsConstructor
@AllArgsConstructor
public class CreateProjectWithSourceResponse {
    private ProjectResponse project;
    private UploadResponse upload;
}
