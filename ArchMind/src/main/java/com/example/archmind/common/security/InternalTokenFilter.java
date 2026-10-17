package com.example.archmind.common.security;

import com.example.archmind.common.result.Result;
import com.example.archmind.common.result.ResultCode;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import lombok.extern.slf4j.Slf4j;
import org.springframework.web.filter.OncePerRequestFilter;
import tools.jackson.databind.ObjectMapper;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

/**
 * {@code /internal/**} 的门锁：校验请求头 {@code X-Internal-Token} 共享密钥。
 *
 * <p>只拦 {@code /internal/**}，其余路径直接放行。**不走 JWT** —— 调用方是 Agent 服务，
 * 按设计它永远拿不到用户 token；两边只共享一个密钥。</p>
 *
 * <p>非 {@code @Component}：由 SecurityConfig 显式建成 Bean 并挂进过滤器链，
 * 避免被 Spring Boot 当作普通 Servlet Filter 再注册一次。</p>
 */
@Slf4j
public class InternalTokenFilter extends OncePerRequestFilter {

    private static final String HEADER = "X-Internal-Token";
    private static final String PREFIX = "/internal/";

    private final byte[] expectedToken;
    private final ObjectMapper objectMapper;

//注入对象
    public InternalTokenFilter(String token, ObjectMapper objectMapper) {
        this.expectedToken = token.getBytes(StandardCharsets.UTF_8);
        this.objectMapper = objectMapper;
    }
//Filter 过滤器
    /**
     * request：请求对啊行，拿到请求地址，请求头，参数
     * Response：响应对象，用来返回状态码
     * FilterChain：过滤器链 `filterChain.doFilter()` = **放行，继续执行后面的过滤器**
     * 不做保护范围的接口本过滤器不做任何校验,直接交个过滤器下一个Filter
     * 只有命中前缀的内部接口,才强校验这个内部Token
     */

    @Override
    protected void doFilterInternal(HttpServletRequest request,
                                    HttpServletResponse response,
                                    FilterChain filterChain) throws ServletException, IOException {
//        如果请求地址不是以指定的 "/internal/"开头直接方向,不做token检验
//        该过滤器是局部拦截器,只负责保护该internal这一组接口,不是全局鉴权,不是所有接口都要校验这个内部token
        if (!request.getRequestURI().startsWith(PREFIX)) {
            filterChain.doFilter(request, response);
            return;
        }
//从请求头HEADER字段取出HEADER
        String token = request.getHeader(HEADER);
        // 定长比较，避免逐字符短路带来的时序侧信道
//        token不为空并且使用messageDigest.isEqual做字节数组对比
        if (token != null
                && MessageDigest.isEqual(expectedToken, token.getBytes(StandardCharsets.UTF_8))) {
            filterChain.doFilter(request, response);
            return;
        }
//      鉴权失败打印请求方法+请求URL
        log.warn("内部接口鉴权失败: {} {}", request.getMethod(), request.getRequestURI());
//        设置HTTP状态码:SC_UNAUTHORIZED==401
        response.setStatus(HttpServletResponse.SC_UNAUTHORIZED);
//        设置响应类型为JSON
        response.setContentType("application/json;charset=UTF-8");
        objectMapper.writeValue(response.getOutputStream(),
                Result.fail(ResultCode.UNAUTHORIZED.getCode(), "内部接口鉴权失败"));
    }
}
