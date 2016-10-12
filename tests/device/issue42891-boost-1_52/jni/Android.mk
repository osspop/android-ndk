LOCAL_PATH := $(call my-dir)

include $(CLEAR_VARS)
LOCAL_MODULE := sdktest
LOCAL_SRC_FILES := main.cpp
LOCAL_C_INCLUDES := jni/boost
LOCAL_CPPFLAGS := -fexceptions -frtti -DCALL_X
LOCAL_STATIC_LIBRARIES := libandroid_support
include $(BUILD_EXECUTABLE)

$(call import-module,android/support)
