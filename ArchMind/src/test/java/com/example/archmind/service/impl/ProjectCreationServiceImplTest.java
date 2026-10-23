package com.example.archmind.service.impl;

import com.example.archmind.common.exception.BusinessException;
import com.example.archmind.common.util.RedisUtil;
import com.example.archmind.dao.ProjectMapper;
import com.example.archmind.dto.request.ProjectCreateRequest;
import com.example.archmind.dto.response.CreateProjectWithSourceResponse;
import com.example.archmind.dto.response.ProjectResponse;
import com.example.archmind.dto.response.UploadResponse;
import com.example.archmind.entity.Project;
import com.example.archmind.service.ProjectService;
import com.example.archmind.service.UploadService;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.mockito.junit.jupiter.MockitoSettings;
import org.mockito.quality.Strictness;
import org.springframework.web.multipart.MultipartFile;

import java.util.concurrent.TimeUnit;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
class ProjectCreationServiceImplTest {

    private static final String KEY = "idem:project:7:k1";

    @Mock
    private ProjectService projectService;
    @Mock
    private UploadService uploadService;
    @Mock
    private ProjectMapper projectMapper;
    @Mock
    private RedisUtil redisUtil;
    @Mock
    private MultipartFile file;

    private ProjectCreationServiceImpl service;

    @BeforeEach
    void setUp() {
        service = new ProjectCreationServiceImpl(projectService, uploadService, projectMapper, redisUtil);
    }

    private static ProjectCreateRequest request(String name) {
        ProjectCreateRequest req = new ProjectCreateRequest();
        req.setName(name);
        return req;
    }

    private static ProjectResponse projectResponse(long id) {
        return ProjectResponse.builder().id(id).name("p").build();
    }

    @Test
    void createsProjectAndUploadThenStoresIdempotencyResult() {
        when(redisUtil.setIfAbsent(eq(KEY), eq("IN_PROGRESS"), anyLong(), any(TimeUnit.class)))
                .thenReturn(true);
        when(projectService.createProject(eq(7L), any())).thenReturn(projectResponse(99L));
        when(uploadService.upload(eq(99L), any())).thenReturn(UploadResponse.builder().projectId(99L).build());

        CreateProjectWithSourceResponse resp =
                service.createWithSource(7L, request("p"), file, "k1");

        assertEquals(99L, resp.getProject().getId());
        assertNotNull(resp.getUpload());
        verify(uploadService).validateZip(file);
        verify(redisUtil).set(KEY, "99", 10, TimeUnit.MINUTES);
        verify(projectService, never()).deleteProjectData(anyLong());
    }

    @Test
    void replaysExistingProjectForSameIdempotencyKey() {
        when(redisUtil.setIfAbsent(eq(KEY), eq("IN_PROGRESS"), anyLong(), any(TimeUnit.class)))
                .thenReturn(false);
        when(redisUtil.get(KEY)).thenReturn("42");
        Project existing = new Project();
        existing.setId(42L);
        existing.setUserId(7L);
        when(projectMapper.selectById(42L)).thenReturn(existing);

        CreateProjectWithSourceResponse resp =
                service.createWithSource(7L, request("p"), file, "k1");

        assertEquals(42L, resp.getProject().getId());
        assertNull(resp.getUpload());
        verify(projectService, never()).createProject(anyLong(), any());
        verify(uploadService, never()).upload(anyLong(), any());
    }

    @Test
    void rejectsDuplicateWhileFirstRequestInProgress() {
        when(redisUtil.setIfAbsent(eq(KEY), eq("IN_PROGRESS"), anyLong(), any(TimeUnit.class)))
                .thenReturn(false);
        when(redisUtil.get(KEY)).thenReturn("IN_PROGRESS");

        assertThrows(BusinessException.class,
                () -> service.createWithSource(7L, request("p"), file, "k1"));
        verify(projectService, never()).createProject(anyLong(), any());
    }

    @Test
    void rollsBackProjectAndReleasesKeyWhenUploadFails() {
        when(redisUtil.setIfAbsent(eq(KEY), eq("IN_PROGRESS"), anyLong(), any(TimeUnit.class)))
                .thenReturn(true);
        when(projectService.createProject(eq(7L), any())).thenReturn(projectResponse(99L));
        doThrow(new BusinessException("上传处理失败")).when(uploadService).upload(eq(99L), any());

        assertThrows(BusinessException.class,
                () -> service.createWithSource(7L, request("p"), file, "k1"));

        verify(projectService).deleteProjectData(99L);
        verify(redisUtil).delete(KEY);
    }

    @Test
    void validatesBeforeCreatingProject() {
        assertThrows(BusinessException.class,
                () -> service.createWithSource(7L, request("   "), file, "k1"));

        verify(redisUtil, never()).setIfAbsent(anyString(), any(), anyLong(), any(TimeUnit.class));
        verify(projectService, never()).createProject(anyLong(), any());
        verify(uploadService, never()).validateZip(any());
    }
}
