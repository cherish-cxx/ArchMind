package com.example.archmind.service;

import com.example.archmind.dto.response.UploadResponse;
import org.springframework.web.multipart.MultipartFile;

public interface UploadService {

    UploadResponse upload(Long projectId, MultipartFile file);

    /** 只做校验（非空/大小/后缀/zip 魔数），供「创建+上传」在落库前提前失败 */
    void validateZip(MultipartFile file);
}
