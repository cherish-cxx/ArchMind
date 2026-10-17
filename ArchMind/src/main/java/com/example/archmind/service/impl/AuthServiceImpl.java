package com.example.archmind.service.impl;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.example.archmind.common.exception.BusinessException;
import com.example.archmind.common.result.ResultCode;
import com.example.archmind.common.util.JwtUtil;
import com.example.archmind.common.util.RedisUtil;
import com.example.archmind.dao.UserMapper;
import com.example.archmind.dto.request.LoginRequest;
import com.example.archmind.dto.response.LoginResponse;
import com.example.archmind.entity.User;
import com.example.archmind.service.AuthService;
import com.example.archmind.service.UserService;
import lombok.RequiredArgsConstructor;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.stereotype.Component;
import org.springframework.stereotype.Service;

import java.util.concurrent.TimeUnit;

@Service
@RequiredArgsConstructor
public class AuthServiceImpl implements AuthService {

    // ========== Redis Key 前缀 ==========
    private static final String TOKEN_PREFIX = "auth:token:";
    private static final String REFRESH_TOKEN_PREFIX = "auth:refresh:";
    private static final String USER_TOKEN_PREFIX = "auth:user:";
    private static final String BLACKLIST_PREFIX = "auth:blacklist:";


    // ========== 配置参数（从配置文件读取） ==========
    // 这里可以 @Value 注入，为了简洁直接写死，实际从配置文件读取
    private static final Long ACCESS_TOKEN_EXPIRE = 7200L;      // 2 小时
    private static final Long REFRESH_TOKEN_EXPIRE = 604800L;   // 7 天


    private final UserService userService;
    private final JwtUtil jwtUtil;
    private final RedisUtil redisUtil;
    private final PasswordEncoder passwordEncoder;
    @Override
    public LoginResponse login(LoginRequest request ,String clientIp){

        User user = userService.getByUsername(request.getUsername());
        if (user==null){
            throw new BusinessException("用户名没找到");
        }

        if (!passwordEncoder.matches(request.getPassword(),user.getPassword())){
            throw new BusinessException("用户密码错误");
        }

//        token内容需要userid和username
        Long userId = user.getId();
        String username = user.getUsername();
//        生成两个token进行验证
        String accessToken = jwtUtil.generateAccessToken(userId,username);
        String refreshToken  = jwtUtil.generateRefreshToken(userId,username);

//        接下来将Token存放到Redis中
        String tokenKey = TOKEN_PREFIX+accessToken;
        redisUtil.set(tokenKey,userId.toString(),ACCESS_TOKEN_EXPIRE, TimeUnit.SECONDS);

        String refreshKey   = REFRESH_TOKEN_PREFIX+refreshToken;
        redisUtil.set(refreshKey,userId.toString(),REFRESH_TOKEN_EXPIRE,TimeUnit.SECONDS);

        String userTokenKey = USER_TOKEN_PREFIX + userId;
        redisUtil.hset(userTokenKey, accessToken, System.currentTimeMillis());
        // 设置与 refresh token 相同的过期时间
        redisUtil.expire(userTokenKey, REFRESH_TOKEN_EXPIRE, TimeUnit.SECONDS);

//        更新这个用户的登录IP
        userService.updateLoginInfo(userId,clientIp);

        return LoginResponse.builder()
                .userId(userId)
                .username(username)
                .accessToken(accessToken)
                .refreshToken(refreshToken)
                .expiresIn(ACCESS_TOKEN_EXPIRE)
                .build();

    };

    @Override
    public void logout(String token){

        String cleanToken = cleanToken(token);
        try{
//            获取用户的ID和用户名（先解析Token，在token中获取id，和username）
            Long userId  = jwtUtil.getUserIdFromToken(cleanToken);
            String username = jwtUtil.getUsernameFromToken(cleanToken);

//          在缓存中删除该accessToken
            String tokenKey = TOKEN_PREFIX+cleanToken;
            redisUtil.delete(tokenKey);

//            将accessToken从redis中删除后还需要将其拉入黑名单防止
//            获取剩余时间直接拉黑
//
            long remainingTime = jwtUtil.getRemainingTime(cleanToken);
            if (remainingTime>0){
                String blacklistKey = BLACKLIST_PREFIX+cleanToken;
                redisUtil.set(blacklistKey,"logout",remainingTime,TimeUnit.SECONDS);
            }

            // 5. 从用户 Token 列表中移除
            String userTokenKey = USER_TOKEN_PREFIX + userId;
            redisUtil.hdel(userTokenKey, cleanToken);
        }catch (Exception e){
            String tokenKey = TOKEN_PREFIX + cleanToken;
            redisUtil.delete(tokenKey);
        }
    }

    @Override
    public LoginResponse refreshToken(String refreshToken) {
        String cleanToken = cleanToken(refreshToken);

        if (!jwtUtil.validateToken(cleanToken)) {
            throw new BusinessException("token无效");
        }

        String tokenType = jwtUtil.getTokenType(cleanToken);
        if (!"refresh".equals(tokenType)) {
            throw new BusinessException("token无效");
        }

        String refreshKey = REFRESH_TOKEN_PREFIX + cleanToken;
        if (!redisUtil.hasKey(refreshKey)) {
            throw new BusinessException(ResultCode.TOKEN_EXPIRED);
        }

        Long userId = jwtUtil.getUserIdFromToken(cleanToken);
        String username = jwtUtil.getUsernameFromToken(cleanToken);

        User user = userService.getByUsername(username);
        if (user == null) {
            throw new BusinessException("用户不存在");
        }
//            token有效且用户存在那么接下来就是更新token
        String newAccessToken = jwtUtil.generateAccessToken(userId,username);

        String newRefreshToken = jwtUtil.generateRefreshToken(userId,username);

//        将新的token保存到redis
        String newTokenKey = TOKEN_PREFIX + newAccessToken;
        redisUtil.set(newTokenKey, userId.toString(), ACCESS_TOKEN_EXPIRE, TimeUnit.SECONDS);

        String newRefreshKey = REFRESH_TOKEN_PREFIX + newRefreshToken;
        redisUtil.set(newRefreshKey, userId.toString(), REFRESH_TOKEN_EXPIRE, TimeUnit.SECONDS);

        // 8.3 更新用户 Token 列表
        String userTokenKey = USER_TOKEN_PREFIX + userId;
        redisUtil.hset(userTokenKey, newAccessToken, System.currentTimeMillis());
        redisUtil.expire(userTokenKey, REFRESH_TOKEN_EXPIRE, TimeUnit.SECONDS);

        // 9. 删除旧的 Refresh Token（一次性的）
        redisUtil.delete(refreshKey);

        return LoginResponse.builder()
                .userId(userId)
                .username(username)
                .accessToken(newAccessToken)
                .refreshToken(newRefreshToken)
                .expiresIn(ACCESS_TOKEN_EXPIRE)
                .build();
    }

//    校验token是否存在于Redis
    @Override
    public boolean validateTokenFromRedis(String token){
        String cleanToken = cleanToken(token);

        String blacklistKey = BLACKLIST_PREFIX+cleanToken;
        if (redisUtil.hasKey(blacklistKey)){
            return false;
        }

        String tokenKey = TOKEN_PREFIX+cleanToken;
        return redisUtil.hasKey(tokenKey);
    }
//做的只是从token中取出UserId
    public Long getUserIdFromToken(String token){
        String cleanToken =cleanToken(token);

        String tokenKey = TOKEN_PREFIX+cleanToken;
//        从redis中解析token获取Userid
        Object  userIdObject = redisUtil.get(tokenKey);
        if (userIdObject!=null){
            return Long.valueOf(userIdObject.toString());
        }
//        如果redis中没有，从JWT解析
        return jwtUtil.getUserIdFromToken(cleanToken);
    }

//用于去掉token中的bearer前缀
    String cleanToken(String token){
            if (token ==null){
                throw new BusinessException("token不存在");
            }
            if (token.startsWith("Bearer")){
                return token.substring(7);
            }
            return token;
    }

}
