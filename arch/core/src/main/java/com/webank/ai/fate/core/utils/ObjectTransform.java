/*
 * Copyright 2019 The FATE Authors. All Rights Reserved.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package com.webank.ai.fate.core.utils;

import java.lang.reflect.Field;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Modifier;
import java.util.HashMap;
import java.util.Map;
import java.lang.reflect.Method;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.apache.commons.lang3.StringUtils;
import org.apache.logging.log4j.Logger;
import org.apache.logging.log4j.LogManager;

public class ObjectTransform {
    private static final Logger LOGGER = LogManager.getLogger();
    public static Map<String, Object> bean2Map(Object object){
        Map<String, Object> map = null;
        try{
            map = new HashMap<>();
            for (Field field : object.getClass().getDeclaredFields()) {
                int mod = field.getModifiers();
                if(Modifier.isStatic(mod) || Modifier.isFinal(mod)){
                    continue;
                }
                String getter = "get" + field.getName().substring(0, 1).toUpperCase() + field.getName().substring(1);
                Method method = object.getClass().getMethod(getter);
                map.put(field.getName(), method.invoke(object));
            }
        }
        catch (NoSuchMethodException ex){
            LOGGER.error(ex);
        }
        catch (IllegalAccessException ex){
            LOGGER.error(ex);
        }
        catch (InvocationTargetException ex){
            LOGGER.error(ex);
        }
        catch (Exception ex){
            LOGGER.error(ex);
        }
        return map;
    }

    public static String bean2Json(Object object){
        if (object == null){
            return "";
        }
        try{
            return new ObjectMapper().writeValueAsString(object);
        }
        catch (JsonProcessingException ex){
            return "";
        }
    }

    public static Object json2Bean(String json, Class objectType){
        if (StringUtils.isEmpty(json)){
            return null;
        }
        try{
            return new ObjectMapper().readValue(json, objectType);
        }
        catch (Exception ex){
            return null;
        }
    }
}
