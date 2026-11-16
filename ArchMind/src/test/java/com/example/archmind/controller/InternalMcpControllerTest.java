package com.example.archmind.controller;

import com.example.archmind.common.exception.McpAuthException;
import com.example.archmind.dto.response.McpContext;
import com.example.archmind.dto.response.McpPrincipal;
import com.example.archmind.dto.response.McpProjectItem;
import com.example.archmind.service.McpPatService;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

import java.util.List;

import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

@ExtendWith(MockitoExtension.class)
class InternalMcpControllerTest {

    @Mock
    private McpPatService service;

    private MockMvc mvc;

    @BeforeEach
    void setUp() {
        mvc = MockMvcBuilders.standaloneSetup(new InternalMcpController(service)).build();
    }

    @Test
    void unknownTokenReturns401() throws Exception {
        when(service.authenticate("bad")).thenThrow(new McpAuthException(401, "无效 token"));

        mvc.perform(post("/internal/mcp/context")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"token\":\"bad\",\"projectId\":1}"))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.code").value(401))
                .andExpect(jsonPath("$.message").value("无效 token"));
    }

    @Test
    void foreignProjectReturns403() throws Exception {
        McpPrincipal principal = McpPrincipal.builder().patId(1L).userId(7L).build();
        when(service.authenticate("good")).thenReturn(principal);
        when(service.context(principal, 99L)).thenThrow(new McpAuthException(403, "无权访问该项目"));

        mvc.perform(post("/internal/mcp/context")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"token\":\"good\",\"projectId\":99}"))
                .andExpect(status().isForbidden())
                .andExpect(jsonPath("$.code").value(403))
                .andExpect(jsonPath("$.message").value("无权访问该项目"));
    }

    @Test
    void missingProjectReturns404() throws Exception {
        McpPrincipal principal = McpPrincipal.builder().patId(1L).userId(7L).build();
        when(service.authenticate("good")).thenReturn(principal);
        when(service.context(principal, 404L)).thenThrow(new McpAuthException(404, "项目不存在"));

        mvc.perform(post("/internal/mcp/context")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"token\":\"good\",\"projectId\":404}"))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.code").value(404));
    }

    @Test
    void validTokenReturnsContext() throws Exception {
        McpPrincipal principal = McpPrincipal.builder().patId(1L).userId(7L).build();
        McpContext ctx = McpContext.builder()
                .userId(7L)
                .patId(1L)
                .projectId(123L)
                .projectName("hmall")
                .analysisStatus("COMPLETED")
                .graphRev(9L)
                .build();
        when(service.authenticate("good")).thenReturn(principal);
        when(service.context(principal, 123L)).thenReturn(ctx);

        mvc.perform(post("/internal/mcp/context")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"token\":\"good\",\"projectId\":123}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.userId").value(7))
                .andExpect(jsonPath("$.patId").value(1))
                .andExpect(jsonPath("$.projectId").value(123))
                .andExpect(jsonPath("$.projectName").value("hmall"))
                .andExpect(jsonPath("$.analysisStatus").value("COMPLETED"))
                .andExpect(jsonPath("$.graphRev").value(9))
                .andExpect(jsonPath("$.token").doesNotExist());
    }

    @Test
    void blankTokenReturns401WithoutCallingService() throws Exception {
        mvc.perform(post("/internal/mcp/context")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"token\":\"   \",\"projectId\":1}"))
                .andExpect(status().isUnauthorized());

        verifyNoInteractions(service);
    }

    @Test
    void missingTokenReturns401WithoutCallingService() throws Exception {
        mvc.perform(post("/internal/mcp/context")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"projectId\":1}"))
                .andExpect(status().isUnauthorized());

        verifyNoInteractions(service);
    }

    @Test
    void projectsReturnsList() throws Exception {
        when(service.projects(7L)).thenReturn(List.of(
                McpProjectItem.builder()
                        .projectId(123L)
                        .name("hmall")
                        .analysisStatus("COMPLETED")
                        .graphRev(9L)
                        .build()));

        mvc.perform(get("/internal/mcp/projects").param("userId", "7"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(0))
                .andExpect(jsonPath("$.data[0].projectId").value(123))
                .andExpect(jsonPath("$.data[0].name").value("hmall"))
                .andExpect(jsonPath("$.data[0].analysisStatus").value("COMPLETED"))
                .andExpect(jsonPath("$.data[0].graphRev").value(9));
    }
}
